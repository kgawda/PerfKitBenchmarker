import logging
import threading

from absl import flags
from perfkitbenchmarker import network
from perfkitbenchmarker import providers
from perfkitbenchmarker import vm_util
from perfkitbenchmarker import errors
from perfkitbenchmarker import virtual_machine
from perfkitbenchmarker.providers.vmware import util
from perfkitbenchmarker.providers.vmware.util import VMwareOrganizationCache

from pyvcloud.vcd.org import Org
from pyvcloud.vcd.vdc_network import VdcNetwork
from pyvcloud.vcd.dhcp_pool import DhcpPool
from pyvcloud.vcd.nat_rule import NatRule
from pyvcloud.vcd.client import TaskStatus
from pyvcloud.vcd.exceptions import UnauthorizedException
from pyvcloud.vcd.exceptions import BadRequestException

FLAGS = flags.FLAGS


class VMwareNetwork(network.BaseNetwork):
    """Object representing a WMware Network (at least for Cloud Director)."""
    CLOUD = providers.VMWARE

    def __init__(self, spec):
        super().__init__(spec)
        self._lock = threading.Lock()
        self.created = False
        if not self.cidr.endswith(".0/24"):
            raise NotImplementedError("Subnets other than /24 not implemented")

        org_cache = VMwareOrganizationCache()
        self.client = org_cache.get_client()
        self.org = org_cache.get_org()
        _, self.vdc, self.edge = org_cache.get_vdcs_and_edge()
        self.ext_net_internet = org_cache.get_ext_net_internet()
        self.network_name = 'perfkit-network-%s' % FLAGS.run_uri
        self.pyvcloud_network = None

        subnet_base = self.cidr.split('/')[0].rsplit('.', 1)[0]
        self.dhcp_range = f"{subnet_base}.100-{subnet_base}.199"
        self.default_gateway = subnet_base+".1"
        logging.debug("Setting dhcp range %r, def. gw. %r", self.dhcp_range, self.default_gateway)

    # @vm_util.Retry(max_retries=3)
    def _Create(self):
        logging.debug("Network _Create. Name %r", self.network_name)
        network = {
            "network_name": self.network_name,
            "network_cidr": util.first_usable_ip_in_subnet_in_cidr_notation(self.cidr),
            "gateway_name": self.edge.name,
            "description": "Network created by PerfKitBenchmarker",
            "is_shared": True,
        }
        network_resource = self.vdc.create_routed_vdc_network(**network)
        task = self.client.get_task_monitor().wait_for_success(task=network_resource.Tasks.Task[0])
        # assert task.get('status') == TaskStatus.SUCCESS.value
        logging.debug("Network creation result: %s", task.get('status'))
        self.pyvcloud_network = VdcNetwork(self.client, resource=network_resource)
        self.CreateDHCPPool()
        self.CreateSNAT()

    def Create(self):
        logging.debug("Network Create. Name %r", self.network_name)

        with self._lock:
            if self.created:
                return
            self._Create()
            self.created = True

    def CreateDHCPPool(self):
        logging.debug("Network CreateDHCPPool")
        primary_server, secondary_server = util.get_recommended_dnses()
        
        self.edge.add_dhcp_pool(
            ip_range=self.dhcp_range,
            default_gateway=self.default_gateway,
            primary_server=primary_server,
            secondary_server=secondary_server
        )
        # TODO: check if enabling dhcp server needed

    def DeleteDHCPPool(self):
        for pool_info in self.edge.list_dhcp_pools():
            if pool_info['IP_Range'] == self.dhcp_range:
                logging.info(f"Remove DHCP Pool {pool_info['ID']} ({pool_info['IP_Range']})")
                pool = DhcpPool(self.client, self.edge.name, pool_info['ID'])
                pool.delete_pool()

    def CreateSNAT(self):
        logging.info("Create SNAT rule " + self.network_name + "-snat")
        self.edge.add_nat_rule(
            action='snat',
            original_address=self.cidr,
            translated_address=self.select_public_address(),
            description=self.network_name + "-snat",
            logging_enabled=True,
            protocol="tcp")
    
    def DeleteSNAT(self):
        nat_rules_resource = self.edge.get_nat_rules()
        if hasattr(nat_rules_resource.natRules, 'natRule'):
            for nat_rule in nat_rules_resource.natRules.natRule:
                if hasattr(nat_rule, 'description') and nat_rule.description == self.network_name + "-snat":
                    logging.info("Delete rule %s", nat_rule.ruleId)
                    resource = NatRule(self.client, self.edge.name, nat_rule.ruleId)
                    resource.delete_nat_rule()

    def select_public_address(self):
        alloc = self.edge.list_external_network_ip_allocations()  # Ex: {'extnw1': ['10.10.10.2'...]}
        ips = alloc.get(self.ext_net_internet)
        logging.debug("External network %s has IPs: %s", self.ext_net_internet, ips)
        if not ips:
            raise errors.Error("Did not found public IP to use")
        return ips[0]

    def _Delete(self):
        """Deletes the actual network."""
        logging.debug("Network _Delete. Name %r", self.network_name)

        # TODO: generalize re-authentication
        try:
            self.vdc.reload()
        except UnauthorizedException:
            logging.info("Re-authenticating")
            util.VMwareOrganizationCache().authenticate_client()
            self.vdc.reload()

        self.DeleteSNAT()
        #TODO DeleteDNAT()
        self.DeleteDHCPPool()
        result = self.vdc.delete_routed_orgvdc_network(
            name=self.network_name, force=True)
        task = self.client.get_task_monitor().wait_for_success(
            task=result)
        # assert task.get('status') == TaskStatus.SUCCESS.value
        logging.debug("Network delete result: %s", task.get('status'))

    def Delete(self):
        logging.info("Network Delete. Name %r", self.network_name)
        with self._lock:
            if not self.created:
                return
            self._Delete()
            self.created = False


class VMwareFirewall(network.BaseFirewall):
    CLOUD = providers.VMWARE

    def __init__(self):
        org_cache = VMwareOrganizationCache()
        self.org = org_cache.get_org()
        _, self.vdc, self.edge = org_cache.get_vdcs_and_edge()
        self.ext_net_internet = org_cache.get_ext_net_internet()

    @vm_util.Retry(max_retries=3)
    def CreatePortForwarding(self, vm:virtual_machine.BaseVirtualMachine, original_port: int, translated_port: int):
        # TODO: extend to UDP, add other options..
        logging.info("Firewall CreatePortForwarding %s:%d -> %s:%d", vm.ip_address, original_port, vm.internal_ip, translated_port)

        try:
            self.edge.add_nat_rule(
                action='dnat',
                original_address=vm.ip_address,
                translated_address=vm.internal_ip,
                description='perfkit-%s-%d-%d-%d' % (FLAGS.run_uri, vm.instance_number, original_port, translated_port),
                #type='User',
                logging_enabled=True,
                #enabled=True,
                #vnic=vnic,
                protocol="tcp",
                original_port=original_port,
                translated_port=translated_port)
            # Possible HTTP Error 400:  <error><errorCode>400</errorCode><details>The entity Ref: com.vmware.vcloud.entity.gateway:29...6b is busy completing an operation NSX_PROXY_CONFIGURE_SERVICES.</details><rootCauseString>The entity Ref: com.vmware.vcloud.entity.gateway:29...6b is busy completing an operation NSX_PROXY_CONFIGURE_SERVICES.</rootCauseString></error>
        except UnauthorizedException:  # TODO: generalize re-authentication
            util.VMwareOrganizationCache().authenticate_client()
            raise errors.Resource.RetryableDeletionError("Re-authentication was needed")
        except BadRequestException as e:
            if e.vcd_error.get('minorErrorCode') == "BUSY_ENTITY":
                raise errors.Resource.RetryableDeletionError("Resource is busy")  # Need to retry implicitly here?
            else:
                raise
            
        logging.debug("Firewall CreatePortForwarding done")

    # def AllowICMP(self, vm, icmp_type=-1, icmp_code=-1, source_range=None):

    def AllowPort(self, vm, start_port, end_port=None, source_range=None):
        super().AllowPort(vm, start_port, end_port)

    def DisallowAllPorts(self):
        """Closes all ports on the firewall."""
        pass

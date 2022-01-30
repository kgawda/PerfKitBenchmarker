# Copyright 2021 PerfKitBenchmarker Authors. All rights reserved.
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import json
import random
import time

import yaml
from absl import flags
from pyvcloud.vcd.client import BasicLoginCredentials
from pyvcloud.vcd.client import Client
from pyvcloud.vcd.client import FenceMode
from pyvcloud.vcd.client import QueryResultFormat
from pyvcloud.vcd.org import Org
from pyvcloud.vcd.system import System
from pyvcloud.vcd.utils import to_dict
from pyvcloud.vcd.vdc import VDC
from pyvcloud.vcd.vapp import VApp
from pyvcloud.vcd.vm import VM
from pyvcloud.vcd.exceptions import EntityNotFoundException
from pyvcloud.vcd.exceptions import BadRequestException
from pyvcloud.vcd.exceptions import UnauthorizedException

from perfkitbenchmarker import custom_virtual_machine_spec
from perfkitbenchmarker import disk
from perfkitbenchmarker import errors
from perfkitbenchmarker import providers
from perfkitbenchmarker import virtual_machine
from perfkitbenchmarker import linux_virtual_machine
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.providers.vmware import util
from perfkitbenchmarker.providers.vmware.util import VMwareOrganizationCache
from perfkitbenchmarker.providers.vmware import vmware_vcd_network
from perfkitbenchmarker.providers.vmware import vmware_vcd_disk
from perfkitbenchmarker.configs import option_decoders


FLAGS = flags.FLAGS


CUST_SCRIPT_TEMPLATE = """#!/bin/bash
if [[ x$1 == xprecustomization ]]; then
    echo Do Nothing
elif [[ x$1 == xpostcustomization ]]; then
    {installs}
    groupadd -fr sudo
    useradd --home {userdir} --shell /bin/bash -m {user}
    usermod -a -G sudo {user}
    mkdir -p {userdir}/.ssh
    echo {key} >> {userdir}/.ssh/authorized_keys
    chown -R {user}:{user} {userdir}/.ssh
    chmod 700 {userdir}/.ssh
    chmod 600 {userdir}/.ssh/authorized_keys
    restorecon -R -v {userdir}/.ssh
    echo "%sudo ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers
    echo "127.0.0.1 {hostname}" >> /etc/hosts
fi
"""


class VMwareVMSpec(virtual_machine.BaseVmSpec):
    CLOUD = providers.VMWARE

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @classmethod
    def _GetOptionDecoderConstructions(cls):
        result = super(VMwareVMSpec, cls)._GetOptionDecoderConstructions()
        result['machine_type'] = (custom_virtual_machine_spec.MachineTypeDecoder, {'default': None})
        return result


class VMwareVirtualMachine(virtual_machine.BaseVirtualMachine):
    """Object representing a Cloud Director Virtual Machine."""
    CLOUD = providers.VMWARE
    DEFAULT_IMAGE = None

    def __init__(self, vm_spec):
        """Initialize virtual machine.

        Args:
          vm_spec: virtual_machine.BaseVirtualMachineSpec object of the vm.
        """
        super().__init__(vm_spec)
        logging.debug("VM __init__ machine type %s", self.machine_type)

        if isinstance(self.machine_type, custom_virtual_machine_spec.CustomMachineTypeSpec):
            self.cpus = self.machine_type.cpus
            self.memory = self.machine_type.memory  # expecting MB
            # self.machine_type = None
        else:
            self.cpus = None
            self.memory = None

        org_cache = VMwareOrganizationCache()
        self.client = org_cache.get_client()
        self.organization = org_cache.get_org()
        self.catalog, self.image = util.select_catalog_image(self.organization, self.OS_TYPE)
        self.vdc, self.network_vdc, self.edge = org_cache.get_vdcs_and_edge()
        self.vapp = None
        self.pyvcloud_vm = None

        self.cidr = util.select_free_cidr()
        self.vapp_name = self.name
        self.max_local_disks = 1
        self.local_disk_counter = 0
        self.image = self.image or self.DEFAULT_IMAGE

    # @vm_util.Retry(max_retries=3)
    def _CreateDependencies(self):
        """Create VM dependencies."""
        logging.debug("VM _CreateDependencies")
        self.firewall = vmware_vcd_network.VMwareFirewall.GetFirewall()
        self.network = vmware_vcd_network.VMwareNetwork.GetNetwork(self)
        logging.info("VM Dependencies: create network")
        self.network.Create()
        logging.debug("VM Dependencies: create network done")

    def _Create(self):
        """Create VM instance."""
        logging.info("Create VM %s", self.name)
        vapp_cfg = {
            'name': self.name,
            'hostname': self.name,
            'vm_name': self.name,
            'catalog': self.catalog,
            'template': self.image,
            'network': self.network.network_name,
            # 'ip_allocation_mode': "dhcp",
            'power_on': False,
            'deploy': False,
            'password': self.password,
            'cust_script': self.get_customization_script(),
            #memory=None,
            #cpu=None,
            #disk_size=None,
            'storage_profile': util.get_storage_policy_name(),
            'network_adapter_type': 'VMXNET3',
        }
        self.vdc.reload()
        vapp_resource = self.vdc.instantiate_vapp(**vapp_cfg)
        # Bug? Customization data not saved (for CentOS?)
        # This creates VM with "PCNet32" NIC if network_adapter_type is not given.
        logging.debug("Waiting for instantiate_vapp")
        self.client.get_task_monitor().wait_for_success(vapp_resource.Tasks.Task[0])

        self.vapp = VApp(self.client, resource=vapp_resource)
        self.pyvcloud_vm = VM(self.client, resource=self.vapp.get_all_vms()[0])
        
        ### if NIC type is "PCNet32" (if network_adapter_type not used)
        # # Delete NIC
        # self.pyvcloud_vm.reload()
        # task = self.pyvcloud_vm.delete_nic(0)
        # self.client.get_task_monitor().wait_for_success(task=task)
        # 
        # # Add new NIC
        # self.pyvcloud_vm.reload()
        # task = self.pyvcloud_vm.add_nic(
        #     adapter_type='VMXNET3',
        #     is_primary=True, 
        #     is_connected=True, 
        #     network_name=self.network.network_name,
        #     ip_address_mode='DHCP',
        #     ip_address=None)
        #     #This disables customization (in xml GuestCustomizationSection)?
        # self.client.get_task_monitor().wait_for_success(task=task)

        ### if NIC type is "PCNet32" - alternative, not tested
        # task = self.pyvcloud_vm.update_nic(self.network.network_name, adapter_type='VMXNET3')
        # result = self.client.get_task_monitor().wait_for_success(task=task)
        # logging.info("Changed NIC type: %s", result.get('status'))

        # Re-enable customization
        logging.debug("Updating VM config")
        self.pyvcloud_vm.reload()

        if self.cpus:
            task = self.pyvcloud_vm.modify_cpu(self.cpus)
            self.client.get_task_monitor().wait_for_success(task=task)
            self.pyvcloud_vm.reload()
        if self.memory:
            task = self.pyvcloud_vm.modify_memory(self.memory)  # in MB
            self.client.get_task_monitor().wait_for_success(task=task)
            self.pyvcloud_vm.reload()
        
        # Did not work:
        # self.pyvcloud_vm.customize_at_next_power_on()

        task = self.pyvcloud_vm.update_guest_customization_section(
            enabled=True,
            customization_script=self.get_customization_script()
        )
        # 400 Error, see: https://github.com/vmware/pyvcloud/issues/635
        # ...and https://github.com/vmware/pyvcloud/pull/789
        self.client.get_task_monitor().wait_for_success(task=task)
        self.pyvcloud_vm.reload()
        
        task = self.pyvcloud_vm.enable_guest_customization(is_enabled=True)  # this works, but for some OS we need to set all customization data again
        self.client.get_task_monitor().wait_for_success(task=task)
        self.pyvcloud_vm.reload()

        task = self.pyvcloud_vm.edit_hostname(hostname=self.name)
        self.client.get_task_monitor().wait_for_success(task=task)
        self.pyvcloud_vm.reload()

        logging.info("Starting VM")
        # task = self.pyvcloud_vm.power_on()  # for some images it does not start customization
        task = self.pyvcloud_vm.power_on_and_force_recustomization()
        self.client.get_task_monitor().wait_for_success(task=task)
        while self.pyvcloud_vm.get_guest_customization_status() == 'GC_PENDING':
            time.sleep(2)
        logging.info("VM Started")

    def _GetInternalIP(self):
        internal_ip = None
        for n in range(40):  # what time limit to use?
            self.pyvcloud_vm.reload()
            nics = self.pyvcloud_vm.list_nics()  # list of dictionaries
            logging.debug("Network checking NICs (%d): %r", n, nics)
            for nic_data in nics:
                if "ip_address" in nic_data:
                    internal_ip = nic_data["ip_address"]
                    logging.info("VM got internal IP: %s", internal_ip)
                    if ':' in internal_ip:
                        # raise errors.Resource.RetryableCreationError('VM didnt get IPv4 address')
                        logging.warning('VM %r got IPv6 address %r instead of IPv4', self.name, internal_ip)
                        continue
                    return internal_ip
            time.sleep(5)

        raise errors.Resource.RetryableCreationError('VM didnt get IP address')

    # @vm_util.Retry()
    def _PostCreate(self):
        """Get the instance's data."""
        
        self.internal_ip = self._GetInternalIP()
        self.ssh_port = util.select_external_tcp_port()
        self.remote_access_ports = [self.ssh_port]
        self.primary_remote_access_port = self.ssh_port

        self.ip_address = self.network.select_public_address()
        # self.AllowRemoteAccessPorts() is called anyway
        self.firewall.CreatePortForwarding(self, self.ssh_port, 22)

    def _Delete(self):
        """Delete a VM instance."""
        logging.info("Delete VM %s", self.name)

        try:
            task = self.vdc.delete_vapp(self.name, force=True)
            self.client.get_task_monitor().wait_for_success(task=task)
        except EntityNotFoundException:
            pass
        except UnauthorizedException:  # TODO: generalize re-authentication
            util.VMwareOrganizationCache().authenticate_client()
            raise errors.Resource.RetryableDeletionError("Re-authentication was needed")
        except BadRequestException as e:
            if e.vcd_error.get('minorErrorCode') == "BUSY_ENTITY":
                raise errors.Resource.RetryableDeletionError("Resource is busy")  # Need to retry implicitly here?
            else:
                raise
        return

    def _Exists(self):
        """Returns true if the VM exists."""

        try:
            vapp = self.vdc.get_vapp(self.name)
        except EntityNotFoundException:
            vapp = None
    
        return vapp is not None

    def CreateScratchDisk(self, disk_spec):
        """Create a VM's scratch disk.
        
        Args:
            disk_spec: virtual_machine.BaseDiskSpec object of the disk.
        """
        logging.debug("CreateScratchDisk %r", disk_spec)
        spec_fields = ('device_path',
                       'disk_number',
                       'disk_size',
                       'disk_type',
                       'mount_point',
                       'num_striped_disks',)
        for spec_field in spec_fields:
            logging.debug(f".. spec {spec_field} is {getattr(disk_spec, spec_field)}")

        disks = []
        for _ in range(disk_spec.num_striped_disks):
            data_disk = vmware_vcd_disk.VMwareDisk(disk_spec, self.vdc)
            data_disk.disk_number = self.remote_disk_counter + 1
            self.remote_disk_counter += 1
            disks.append(data_disk)
        self._CreateScratchDiskFromDisks(disk_spec, disks)

    def get_customization_script(self):
        # Load pubic key
        with open(self.ssh_public_key) as f:
            public_key = f.read().rstrip("\n")

        if isinstance(self, linux_virtual_machine.BaseDebianMixin):
            installs = "apt install -y sudo software-properties-common"
        elif isinstance(self, linux_virtual_machine.BaseRhelMixin):
            installs = "yum install -y sudo"
        else:
            raise NotImplementedError("Unsupported OS type")

        if self.user_name == "root":
            userdir = "/root"
        else:
            userdir = "/home/" + self.user_name

        return CUST_SCRIPT_TEMPLATE.format(key=public_key, user=self.user_name, hostname=self.name, userdir=userdir, installs=installs)


class Ubuntu1604BasedVMwareVirtualMachine(
    VMwareVirtualMachine, linux_virtual_machine.Ubuntu1604Mixin
):
    pass

class Ubuntu1804BasedVMwareVirtualMachine(
    VMwareVirtualMachine, linux_virtual_machine.Ubuntu1804Mixin
):
    pass
    #Does not support script customization in some VMware versions!

class Centos8BasedVMwareVirtualMachine(
    VMwareVirtualMachine, linux_virtual_machine.CentOs8Mixin
):
    pass

class Centos7BasedVMwareVirtualMachine(
    VMwareVirtualMachine, linux_virtual_machine.CentOs7Mixin
):
    pass

class Debian10BasedVMwareVirtualMachine(
    VMwareVirtualMachine, linux_virtual_machine.Debian10Mixin
):
    pass

class Debian9BasedVMwareVirtualMachine(
    VMwareVirtualMachine, linux_virtual_machine.Debian9Mixin
):
    pass

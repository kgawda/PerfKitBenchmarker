
from absl import flags
import logging

from pyvcloud.vcd.client import ResourceType
from pyvcloud.vcd.vapp import VApp

from perfkitbenchmarker import disk
from perfkitbenchmarker import errors
from perfkitbenchmarker.providers.vmware import util

FLAGS = flags.FLAGS


class VMwareDisk(disk.BaseDisk):

    def __init__(self, disk_spec, vdc):
        # if disk_spec.num_striped_disks != 1:
        #     raise ValueError('Striping disks together currently not implemented.')
        super().__init__(disk_spec)
        self.vdc = vdc
        self.pyvcloud_disk = None
        self.vm = None
        logging.debug("Disk __init__: %s %s %s %s %s", self.disk_size, self.disk_type, self.mount_point, self.num_striped_disks, self.metadata)

    def _Create(self):
        self.volume_name = 'pkb-%s-%s' % (FLAGS.run_uri, self.disk_number)
        logging.debug("Disk create %r", self.volume_name)

        disk_resource = self.vdc.create_disk(
            name=self.volume_name,
            size=self.disk_size * 1024**3,
            # description=description,
            storage_profile_name=util.get_storage_policy_name(),
            # iops=iops
            )
        self.vdc.client.get_task_monitor().wait_for_success(disk_resource.Tasks.Task[0])
        self.pyvcloud_disk = disk_resource

    def _Delete(self):
        self.vdc.reload()
        task = self.vdc.delete_disk(disk_id=self.pyvcloud_disk.get('id'))
        self.vdc.client.get_task_monitor().wait_for_success(task=task)

    def Attach(self, vm):
        logging.debug("Disk Attach")
        self.vm = vm
        vapp_resource = vm.vdc.get_vapp(vm.name)
        vapp = VApp(vm.client, resource=vapp_resource)
        disk_href = self.pyvcloud_disk.get('href')
        task = vapp.attach_disk_to_vm(disk_href=disk_href, vm_name=vm.name)
        self.vdc.client.get_task_monitor().wait_for_success(task=task)

        # TODO: move to Linux VM
        cmd = 'for hostdir in /sys/class/scsi_host/host*; do echo "- - -" | sudo tee $hostdir/scan > /dev/null; done; '
        vm.RemoteHostCommand(cmd)

    def Detach(self):
        logging.debug("Disk Detach")
        vapp_resource = self.vdc.get_vapp(self.vm.name)
        vapp = VApp(self.vdc.client, resource=vapp_resource)
        disk_href = self.pyvcloud_disk.get('href')
        task = vapp.detach_disk_from_vm(disk_href=disk_href, vm_name=self.vm.name)
        self.vdc.client.get_task_monitor().wait_for_success(task=task)
        self.vm = None

    def GetDevicePath(self):
        logging.debug("Disk GetDevicePath")
        # TODO: use approach as in Azure
        disk_letter = "abcdefghijklmnopqrstuvwxyz"[self.disk_number]
        logging.debug("Use disk letter %s", disk_letter)
        return '/dev/sd%s' % disk_letter

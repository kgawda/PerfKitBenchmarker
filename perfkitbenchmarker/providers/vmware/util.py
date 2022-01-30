# Copyright 2015 PerfKitBenchmarker Authors. All rights reserved.
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
import random
import yaml
from statistics import geometric_mean

from absl import flags
from pyvcloud.vcd.client import BasicLoginCredentials
from pyvcloud.vcd.client import Client
from pyvcloud.vcd.org import Org
from pyvcloud.vcd.vdc import VDC
from pyvcloud.vcd.gateway import Gateway
import pyvcloud.vcd.utils


FLAGS = flags.FLAGS


def get_predefined_cloud_data(vcd_cloud):
    if vcd_cloud:
        with open("perfkitbenchmarker/providers/vmware/vmware_vcd_clouds.yml") as f:
            vcd_providers = yaml.safe_load(f)
        if vcd_cloud in vcd_providers:
            return vcd_providers[vcd_cloud]
        for provider_data in vcd_providers.values():
            if provider_data.get("name", "") == vcd_cloud:
                return provider_data
            if provider_data.get("abbreviation", "") == vcd_cloud:
                return provider_data


class VMwareOrganizationCache:
    class VMwareOrganizationCacheData:
        client = None
        org = None
        compute_vdc = None
        network_vdc = None
        edge = None
        ext_net_internet = None
    
    _cache = VMwareOrganizationCacheData()

    def get_client(self):
        if not self._cache.client:
            self._cache.client = self._get_pyvcloud_client()
            self.authenticate_client()
        return self._cache.client

    def get_org(self):
        client = self.get_client()
        if not self._cache.org:
            self._cache.org = Org(client, resource=client.get_org())
        return self._cache.org

    def get_vdcs_and_edge(self):
        self._set_vdcs_and_networking()
        return self._cache.compute_vdc, self._cache.network_vdc, self._cache.edge

    def get_ext_net_internet(self):
        self._set_vdcs_and_networking()
        return self._cache.ext_net_internet

    def authenticate_client(self):
        logging.info("Logging in to organization %s as %s", FLAGS.VCD_ORG, FLAGS.VCD_USER)
        self._cache.client.set_credentials(BasicLoginCredentials(FLAGS.VCD_USER, FLAGS.VCD_ORG, FLAGS.VCD_PASSWORD))

    def _set_vdcs_and_networking(self):
        if not self._cache.client:
            self.get_client()
        if not all((self._cache.ext_net_internet, self._cache.compute_vdc, self._cache.network_vdc, self._cache.edge)):
            self._cache.ext_net_internet, self._cache.compute_vdc, self._cache.network_vdc, self._cache.edge = self._select_vdcs_edge_extnet()

    def _get_pyvcloud_client(self):
        predefined = get_predefined_cloud_data(FLAGS.VCD_CLOUD)
        host = FLAGS.VCD_HOST or predefined["host"]
        port = FLAGS.VCD_PORT or predefined["port"]
        logging.debug("Creating vCD client for host %s, port %s", host, port)
        api_version = FLAGS.VCD_API_VERSION or predefined.get("api_version", None)  # None means: use default
        if port != 443:
            if host.endswith('/'):
                host = host[:-1]
            host += "{}:{}".format(host, port)
        verify_ssl = FLAGS.VCD_VERIFY_SSL or predefined["verify_ssl"]
        
        client = Client(
            host,
            verify_ssl_certs=verify_ssl,
            api_version=api_version,
            log_file='pyvcloud.log',
            log_requests=True,
            log_headers=True,
            log_bodies=True)
        return client

    @staticmethod
    def _vdc_rank(vdc_resource):
        resource_normalization_ratios = {
            "GB": 1000.0,
            "MB": 1.0,
            "kB": 0.001,
            "GHz": 2000.0,
            "MHz": 2.0,
        }
        factors = []
        # For fields vdc_resource fields see https://github.com/vmware/pyvcloud/blob/master/pyvcloud/vcd/utils.py#L99
        for resource in (vdc_resource.ComputeCapacity.Memory, vdc_resource.ComputeCapacity.Cpu):
            f = (max(resource.Allocated, resource.Limit) - resource.Used) * resource_normalization_ratios[resource.Units]
            factors.append(f)
        return geometric_mean(factors)

    def _iter_vdc_edge_extnet(self, vdcs, preferred_vdc_name=""):
        client = self.get_client()
        org = self.get_org()
        preferred_name_key = lambda vdc_info: vdc_info['name'] != preferred_vdc_name            
        for vdc_info in sorted(vdcs, key=preferred_name_key):
            vdc = VDC(client, resource=org.get_vdc(vdc_info['name']))        
            for edge_data in vdc.list_edge_gateways():
                edge = Gateway(client, href=edge_data['href'])
                for net_name in edge.list_external_network_ip_allocations():
                    yield vdc, edge, net_name

    def _select_networking(self, vdcs, ext_net_name, preferred_vdc_name=""):
        if ext_net_name:
            for vdc, edge, net_name in self._iter_vdc_edge_extnet(vdcs, preferred_vdc_name=preferred_vdc_name):
                if net_name == ext_net_name:
                    return ext_net_name, edge, vdc
            raise Exception(f"No Edge router with external network {ext_net_name} found in in organization")
        else:
            count = 0
            for vdc, edge, net_name in self._iter_vdc_edge_extnet(vdcs, preferred_vdc_name=preferred_vdc_name):
                count += 1
                if "internet" in net_name.lower():
                    logging.debug("Guessed external network: %s", net_name)
                    return net_name, edge, vdc
            if count == 1:  # there was only one, so it's the best guess
                logging.debug("Guessed only available external network: %s", net_name)
                return net_name, edge, vdc
            raise Exception(f"No Edge router with reasonable external network found in in organization")

    def _select_vdcs_edge_extnet(self):
        client = self.get_client()
        org = self.get_org()
        vdcs = org.list_vdcs()

        # Select vDC with most available resources
        best_vdc_info = max(vdcs, key=lambda vdc_info: self._vdc_rank(org.get_vdc(vdc_info['name'])))
        logging.debug(f"Selected best compute vDC: %s", best_vdc_info['name'])
        compute_vdc = VDC(client, resource=org.get_vdc(best_vdc_info['name']))

        # Select vDC containing Edge with external internet network
        extnet = get_predefined_cloud_data(FLAGS.VCD_CLOUD).get("external_network_internet", None)
        extnet, edge, network_vdc = self._select_networking(vdcs, extnet, preferred_vdc_name=compute_vdc.name)

        logging.debug(f"Selected best networking vDC: %s", network_vdc.name)
        logging.debug(f"Selected edge: %s", edge.name)
        edge.reload()  # needed?

        return extnet, compute_vdc, network_vdc, edge


def get_recommended_dnses():
    cloud_data = get_predefined_cloud_data(FLAGS.VCD_CLOUD)
    return cloud_data.get("recomended_dnses", ("8.8.8.8", "8.8.4.4"))


def get_storage_profile():
    return "ssd"


def get_storage_policy_name(storage_profile_type=None):
    if storage_profile_type is None:
        storage_profile_type = get_storage_profile()
    policies = get_predefined_cloud_data(FLAGS.VCD_CLOUD).get("storage_policies", {})
    if storage_profile_type not in policies:
        raise Exception(f"Current cloud has no definition of Storage Policy for {storage_profile_type!r}")
    return policies[storage_profile_type]


def verify_image(org, catalog_name, image_name):
    item = org.get_catalog_item(catalog_name, image_name)
    return item.Entity.get("type") == 'application/vnd.vmware.vcloud.vAppTemplate+xml'    


def _iter_image_names_in_catalogs(org, catalog_names):
    for catalog_name in catalog_names:
        images = org.list_catalog_items(catalog_name)
        for image_data in images:
            yield catalog_name, image_data["name"]


def select_catalog_image(org, os_type):
    def match_image_name(sample_name: str, target_name: str) -> bool:
        to_remove = ("server", "temp", ".", " ", "-")
        sample_name = sample_name.lower()
        for token in to_remove:
            sample_name = sample_name.replace(token, "")
        return sample_name.startswith(target_name)

    cloud_data = get_predefined_cloud_data(FLAGS.VCD_CLOUD)
        
    image_details = cloud_data.get("images", {}).get(os_type, {})

    if "catalog" in image_details and "template" in image_details:
        if not verify_image(org, image_details["catalog"], image_details["template"]):
            raise Exception(f"Given image {image_details['template']} is not a proper VM template.")
        return image_details["catalog"], image_details["template"]
    
    if "catalog" in image_details:
        catalogs = [image_details["catalog"]]
    else:
        catalogs = [c["name"] for c in org.list_catalogs()]
        if not catalogs:
            raise Exception(f"No image catalog in organization {org.get_name()}")

    # Just find proper catalog
    if "template" in image_details:
        for catalog_name, image_name in _iter_image_names_in_catalogs(org, catalogs):
            if image_details["template"] == image_name:
                if verify_image(org, catalog_name, image_name):
                    return catalog_name, image_name
        raise Exception(f"Could not find proper image named {image_details['template']} in organization {org.get_name()}")

    # Guess: compare image names vs values defined in perfkitbenchmarker.os_types
    # e.g.: 'Ubuntu Server 18.04 LTS 64bit' vs "ubuntu1804"
    for catalog_name, image_name in _iter_image_names_in_catalogs(org, catalogs):
        if match_image_name(image_name, os_type):
            if verify_image(org, catalog_name, image_name):
                logging.debug("Guessed image for OS %s: %s ", os_type, image_name)
                return catalog_name, image_name
    
    raise Exception(f"Could not guess image for {os_type} in organization {org.get_name()}")


def select_free_cidr():
    #TODO
    return '192.168.123.0/24'


def first_usable_ip_in_subnet_in_cidr_notation(cidr):
    #TODO
    assert cidr == '192.168.123.0/24'
    return '192.168.123.1/24'


def select_external_tcp_port():
    #TODO
    return random.randrange(9000, 65535)

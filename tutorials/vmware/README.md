VMware cloud provider
=====================

PerfKitBenchmarker module for VMware Cloud Director based clouds.

Prototype version, to be discussed.

Install
-------

For use with Cloud Director you need some modules, including *pyvcloud* (see description below):

```bash
pip install -r perfkitbenchmarker/providers/vmware/requirements.txt
pip install git+https://github.com/vmware/pyvcloud.git@refs/pull/789/head
```

Use
---

Enter credentials and cloud data in environment variables or pkb flags:
  - Needed:
    - VMWARE_API=Director
    - VCD_ORG
    - VCD_USER
    - VCD_PASSWORD
  - If your cloud is included in `perfkitbenchmarker/providers/vmware/vmware_vcd_clouds.yml` then you need only:
    - VCD_CLOUD - name, abbreviation or internal YAML name, as defined in `vmware_vcd_clouds.yml`
  - Otherwise, you need to set:
    - VCD_HOST
    - Optional:
      - VCD_PORT, default 443
      - VCD_VERIFY_SSL, default True
      - VCD_API_VERSION, default: auto-negotiate

Prepare pre-provisioned data form *sample* benchmark:
```bash
echo 1234567890 > preprovisioned_data.txt
```

Run benchmarks, e.g.:
```bash
./pkb.py --cloud=VMware --benchmarks=sample --os_type=debian10
./pkb.py --cloud=VMware --benchmarks=unixbench --os_type=debian10
./pkb.py --cloud=VMware --benchmarks=ping --os_type=debian10 --ip_addresses=INTERNAL --ssh_control_path=/tmp/perfkit-%h-%p-%r
./pkb.py --cloud=VMware --benchmarks=iperf --os_type=debian10 --ip_addresses=INTERNAL --ssh_control_path=/tmp/perfkit-%h-%p-%r
```

Decisions taken
---------------

  - Single provider for different VMware products (at least: vCenter and Cloud Director).
    A configuration variable (VMWARE_API) to select which one to use + set of product-specific variables
    (VCD_ORG, VCD_CLOUD, VCD_HOST).
    This will add another level of complexity for its users (e.g. readme with two options explained).
    On the other hand, it limits the number of providers.
    This is a tough choice.

  - Use *pyvcloud* binding instead of command line tool (*vcd-cli*). 
    - *vcd-cli* does not handle well asynchronous invocations.
      Difficult (if possible at all) to use this tool in multiple parallel runs.
    - *vcd-cli* is based on *pyvcloud*. It does not expose all *pyvcloud*'s features and introduces
      another layer of possible bugs.

  - How to pass credentials: environment variables (or command line options).
    This is simple approach. Additionally, env. variables can be stored in a file, as `openrc.sh` is sometimes used for OpenStack.
    Such env. variables are also used by other tools (e.g. VCD_PASSWORD used by *vcd-cli*).
    Alternative choice would be to read data from file (e.g. re-use *vcd-cli*'s file?).

  - How to pass environment details: `vmware_vcd_clouds.yml` file + smart guess.
    Some details about current cloud environment could be guessed if not given (e.g. image, choice of vDC, Edge, external network) - the choice may depend on specific user and the services they ordered.
    Some details can be seen as "well known" and preconfigured for specific clouds (again: images and external network, but not vDC and Edge).
    It is possible to add third level: flags/env. variables.

  - Use a version of *pyvcloud* based on GitHub pull request instead of PyPi default package.
    I have encountered a bug in *pyvcloud* and needed it fixed for this cloud provider to operate.
    So I created a pull request for it [https://github.com/vmware/pyvcloud/pull/789].
    While it is not merged, I decided to provide it as a source for `pip install`.


Open questions
--------------

  - Public IP shared between multiple VMs - how to deal with it in PerfKit?
    In clouds based on VMware Cloud Directors (at least the ones I have access to) it is typical to have less public IPs available than VMs.
    NAT is utilized to publish specific ports of VMs. This seems to be fine for benchmarks with one VM.

    This revealed a problem with SSH control path - see section below.

    Also, I did not check how to run benchmarks with multiple VMs without `--ip_addresses=INTERNAL`.

  - How to keep cloud runtime data (e.g. client session, guessed vDCs and Edge router)?
    Currently, a `util.py` has a singleton-based `VMwareOrganizationCache` object.
    Would be good to verify this approach with some corner cases (e.g. can one *pkb* run utilize multiple logins/environments for one provider?).

  - How to refresh token?
    If the benchmark runs for some time, the access token looses validity and next API call will result in `pyvcloud.vcd.exceptions.UnauthorizedException`.
    Is there any PerfKit general mechanism for that, or do I need to find some general solution on *pyvcloud* level?

  - Do I need to add this cloud provider to `BENCHMARK_CONFIG` in `perfkitbenchmarker/linux_benchmarks/sysbench_benchmark.py` to use it?
    Currently, I am getting *sysbench* error suggesting that it is missing there.

  - How to select storage performance profiles, e.g. SSD or HDD? Is there some flag for this?


Other problems noted
--------------------

  - There is a problem with SSH control path when multiple VMs share same public IP.
    PerfKit's default for ssh_control_path is `%h` (`vm_util.py` line 206).
    This distinguishes VMs only by public IP.
    The value can be changed to `%h-%p-%r` with command line option like `--ssh_control_path=/tmp/perfkit-%h-%p-%r`.
    However, a permanent change of default value would be helpful.

  - PerfKit verifies `requirements.txt` files on each run.
    However, it does not accept HTTP/Git-based lines, e.g.: `git+https://github.com/vmware/pyvcloud.git@refs/pull/789/head`.
    Trying to use it ends up in `pkg_resources.extern.packaging.requirements.InvalidRequirement`.

  - Some cloud images contain translations of command line tools.
    On Non-English workstation,
    when `LANG` env. variable is being sent by SSH client and accepted by SSH server (default in many cases),
    printouts from some tools are translated. This ruins parsing, obviously.
    Requiring user to change `ssh_config` is not very an elegant solution.
    Some solution would be to provide SSH client with modified env. variables, or forbid passing it to server.
    A work-around using latter approach could be done with adding `SendEnv -LC_* -LANG*` into ssh config [https://bugzilla.mindrot.org/show_bug.cgi?id=1285],
    so in theory also by running ssh with `-o SendEnv -LC_* -LANG*`, and this in turn could be provided to PerfKit with something like
    `--ssh_options="-o","SendEnv","-LC_*","-LANG*"`. It did not work for me.

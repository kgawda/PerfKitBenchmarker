import os
import warnings

from absl import flags


flags.DEFINE_string('VMWARE_API',
                    os.environ.get('VMWARE_API', 'Director'),
                    'VMware product API to use.')

flags.DEFINE_string('VCD_CLOUD',
                    os.environ.get('VCD_CLOUD'),
                    'Name of predefined Cloud Director provider profile.')

flags.DEFINE_string('VCD_HOST',
                    os.environ.get('VCD_HOST', None),
                    'API host for Cloud Director.')

flags.DEFINE_integer('VCD_PORT',
                     os.environ.get('VCD_PORT', 443),
                     'API port for Cloud Director.', lower_bound=0, upper_bound=65535)

flags.DEFINE_boolean('VCD_VERIFY_SSL',
                     os.environ.get('VCD_VERIFY_SSL', True),
                     'API HTTPS certificate validation for Cloud Director.')

flags.DEFINE_string('VCD_USER',
                    os.environ.get('VCD_USER', None),
                    'Username for VMware authentication')

flags.DEFINE_string('VCD_PASSWORD',
                    os.environ.get('VCD_PASSWORD', None),
                    'Password for VMware authentication')

flags.DEFINE_string('VCD_ORG',
                    os.environ.get('VCD_ORG', None),
                    'Organization to use in Cloud Director')

flags.DEFINE_string('VCD_API_VERSION',
                    os.environ.get('VCD_API_VERSION', None),
                    'API host for Cloud Director.')

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message=r"Flag \S+ has a non-None default value; therefore, mark_flag_as_required will pass even if flag is not specified in the command line\!")
    flags.mark_flag_as_required('VCD_USER')
    flags.mark_flag_as_required('VCD_PASSWORD')
    flags.mark_flag_as_required('VCD_ORG')

# -*- coding: utf-8 -*-
#  Licensed under the Apache License, Version 2.0 (the "License"); you may
#  not use this file except in compliance with the License. You may obtain
#  a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#  WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#  License for the specific language governing permissions and limitations
#  under the License.

import eventlet

# NOTE(dims): monkey patch subprocess to prevent failures in latest eventlet
# See https://github.com/eventlet/eventlet/issues/398
try:
    eventlet.monkey_patch(subprocess=True)
except TypeError:
    pass

# -- General configuration ----------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom ones.
extensions = ['sphinx.ext.viewcode',
              'sphinx.ext.graphviz',
              'sphinxcontrib.httpdomain',
              'sphinxcontrib.pecanwsme.rest',
              'sphinxcontrib.seqdiag',
              'sphinxcontrib.apidoc',
              'oslo_config.sphinxext',
              'oslo_config.sphinxconfiggen',
              'oslo_policy.sphinxext',
              'oslo_policy.sphinxpolicygen',
              ]

try:
    import openstackdocstheme
    extensions.append('openstackdocstheme')
except ImportError:
    openstackdocstheme = None

# sphinxcontrib.apidoc options
apidoc_module_dir = '../../ironic'
apidoc_output_dir = 'contributor/api'
apidoc_excluded_paths = [
    'db/sqlalchemy/alembic/env',
    'db/sqlalchemy/alembic/versions/*',
    'drivers/modules/ansible/playbooks*',
    'tests',
]
apidoc_separate_modules = True

repository_name = 'openstack/ironic'
bug_project = '943'
bug_tag = ''

wsme_protocols = ['restjson']

# autodoc generation is a bit aggressive and a nuisance when doing heavy
# text edit cycles.
# execute "export SPHINX_DEBUG=1" in your terminal to disable

# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates']

# The suffix of source filenames.
source_suffix = '.rst'

# The master toctree document.
master_doc = 'index'

# General information about the project.
copyright = u'OpenStack Foundation'

config_generator_config_file = '../../tools/config/ironic-config-generator.conf'
sample_config_basename = '_static/ironic'

policy_generator_config_file = '../../tools/policy/ironic-policy-generator.conf'
sample_policy_basename = '_static/ironic'

# A list of ignored prefixes for module index sorting.
modindex_common_prefix = ['ironic.']

# If true, '()' will be appended to :func: etc. cross-reference text.
add_function_parentheses = True

# If true, the current module name will be prepended to all description
# unit titles (such as .. function::).
add_module_names = True

# The name of the Pygments (syntax highlighting) style to use.
pygments_style = 'sphinx'

# A list of glob-style patterns that should be excluded when looking for
# source files. They are matched against the source file names relative to the
# source directory, using slashes as directory separators on all platforms.
exclude_patterns = ['api/ironic.drivers.modules.ansible.playbooks.*',
                    'api/ironic.tests.*']

# Ignore the following warning: WARNING: while setting up extension
# wsmeext.sphinxext: directive 'autoattribute' is already registered,
# it will be overridden.
suppress_warnings = ['app.add_directive']

# -- Options for HTML output --------------------------------------------------

# The theme to use for HTML and HTML Help pages.  Major themes that come with
# Sphinx are currently 'default' and 'sphinxdoc'.
if openstackdocstheme is not None:
    html_theme = 'openstackdocs'
else:
    html_theme = 'default'

# Output file base name for HTML help builder.
htmlhelp_basename = 'Ironicdoc'


# Grouping the document tree into LaTeX files. List of tuples
# (source start file, target name, title, author, documentclass
# [howto/manual]).
latex_documents = [
    (
        'index',
        'Ironic.tex',
        u'Ironic Documentation',
        u'OpenStack Foundation',
        'manual'
    ),
]

# -- Options for seqdiag ------------------------------------------------------

seqdiag_html_image_format = "SVG"

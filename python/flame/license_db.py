# SPDX-FileCopyrightText: 2024 Henrik Sandklef
#
# SPDX-License-Identifier: GPL-3.0-or-later

import boolean
import collections
import glob
import json
import logging
import re
from pathlib import Path
import license_expression
import spdx_license_list
from enum import Enum

from flame.config import LICENSE_DIR, LICENSE_SCHEMA_FILE, read_config
from flame.config import LICENSE_OPERATORS_FILE, LICENSE_COMPOUNDS_FILE, LICENSE_AMBIG_FILE, LICENSE_DUALS_FILE

from flame.exception import FlameException
from jsonschema import validate

import osadl_matrix

json_schema = None

FLAME_ALIASES_TAG = 'aliases'
FLAME_COMPATIBLE_LICENSE_TAG = 'compat_license'
FLAME_IDENTIFIED_LICENSE_TAG = 'identified_license'
FLAME_LICENSE_TEXT_TAG = 'license_text'
FLAME_NAME_TAG = 'name'
FLAME_COMPATIBILITY_TAG = 'compatibility'

COMPATIBILITY_AS_TAG = 'compatibility_as'
IDENTIFIED_ELEMENT_TAG = 'identified_element'
SCANCODE_KEY_TAG = 'scancode_key'
SCANCODE_KEYS_TAG = 'scancode_keys'
LICENSES_TAG = 'licenses'
COMPATS_TAG = 'compats'
DUALS_TAG = 'dual-licenses'
AMBIG_TAG = 'ambiguties'
DUAL_LICENSES_TAG = 'dual-licenses'
DUAL_NEWER_TAG = 'newer-versions'
COMPOUNDS_TAG = 'compounds'

LICENSE_OPERATORS_TAG = 'license_operators'

# expression for splitting a license expression in to a list of the
# actual license name/id without the "AND", "OR", "(", ")"
#
# Note: OR and AND needs a space around them to prevent splitting for
# example "OReilly" in to "OR eilly"
LICENSE_SPLIT_RE = r'( AND | OR |\(|\))'
LICENSE_SPLIT_RE_CLEAN = r' AND | OR |\(|\) | \||\&'

class Validation(Enum):
    RELAXED = 1
    SPDX = 2
    OSADL = 3
    SCANCODE = 4

class FossLicenses:
    """
    Return a FossLicenses object.
    The config object is checked for the follow variables:
    * check (boolean): enable check of each license against schema
    * license-dir (str): directory where licenses (JSON and LICENSE) are located. Used for testing.
    * additional-license-dir (str): add directory to licenses (JSON and LICENSE) are located. Used for extending flame.
    * logging-level (str): log level to use
    * flame-config (str): configuration file to read settings from


    :param config:
    :raise FlameException: if license_expression is not valid
    :Example:

    >>> fl = FossLicenses()

    """
    def __init__(self, config=None):
        if not config:
            config = {}
        # get config from passed file name via config parameter
        config_from_file = read_config(config.get('flame-config', None))
        # read config from file passed via config parameter
        config_from_file.update((k, v) for k, v in config.items() if v is not None)
        config = config_from_file

        check = config.get('check', False)
        logging_level = self.__str_to_loggin_info(config)
        logging.basicConfig(level=logging_level)

        self.config = config
        self.license_dir = config.get('license-dir', LICENSE_DIR)
        self.license_matrix_file = config.get('license-matrix-file', None)
        self.additional_license_dir = config.get('additional-license-dir', [])
        self.__init_license_db(check)
        self.supported_licenses = None
        self.compat_cache = {}
        self.license_cache = {}
        self.needles_map = {}

    def __str_to_loggin_info(self, config):
        logging_level = config.get('level')
        if not logging_level:
            return logging.INFO
        if logging_level.lower() == "info":
            return logging.INFO
        elif logging_level.lower() == "warning":
            return logging.WARNING
        elif logging_level.lower() == "debug":
            return logging.DEBUG

    def __read_json(self, file_name):
        with open(file_name) as f:
            return json.load(f)

    def __validate_license_data(self, license_data):
        global json_schema
        if not json_schema:
            schema_file = LICENSE_SCHEMA_FILE
            logging.debug(f'Reading JSON schema from {schema_file}')
            json_schema = self.__read_json(schema_file)
        validate(instance=license_data, schema=json_schema)

    def __read_license_file(self, license_file, check=False):
        data = self.__read_json(license_file)
        if check:
            self.__validate_license_data(data)

        license_text_file = license_file.replace('.json', '.LICENSE')
        if not Path(license_text_file).is_file():
            raise FileNotFoundError(f'Could not find "{license_text_file}" matching "{license_file}"')
        with open(license_text_file) as lf:
            data[FLAME_LICENSE_TEXT_TAG] = lf.read()

        return data

    def __init_license_db(self, check=False):
        self.license_db = {}
        licenses = {}
        aliases = {}
        duals = {}
        scancode_keys = {}
        compats = {}
        logging.debug(f'reading from: {self.license_dir}')
        license_dirs = [self.license_dir]
        self.ambiguities = {'ambiguities': [], 'aliases': {}}

        if self.additional_license_dir:
            license_dirs.append(self.additional_license_dir)
        for license_dir in license_dirs:
            for license_file in glob.glob(f'{license_dir}/*.json'):
                if "duals" in license_file:
                    continue
                if "compounds" in license_file:
                    continue
                if "ambig" in license_file:
                    continue
                logging.debug(f'license_file: {license_file}')
                data = self.__read_license_file(license_file, check)
                licenses[data['spdxid']] = data
                for alias in data[FLAME_ALIASES_TAG]:
                    # remove multiple blanks
                    alias = re.sub(' [ ]*', ' ', alias)

                    if alias in aliases:
                        raise FlameException(f'Alias "{alias}" -> {data["spdxid"]} already defined as "{aliases[alias]}".')

                    aliases[alias] = data['spdxid']
                if SCANCODE_KEY_TAG in data:
                    scancode_keys[data[SCANCODE_KEY_TAG]] = data['spdxid']
                if COMPATIBILITY_AS_TAG in data:
                    compats[data['spdxid']] = data[COMPATIBILITY_AS_TAG]

        # Ambiguous licenses
        ambig_file = self.config.get('ambiguity_file', LICENSE_AMBIG_FILE)
        logging.debug(f' * ambiguities file: {ambig_file}')
        data = self.__read_json(ambig_file)
        data['aliases'] = {}

        for k, v in data['ambiguities'].items():
            # for quicker lookups, add 'aliases' which is the reverse of
            # the aliases per license I.e a quicker lookup table when
            # identifying ambiguous licenses
            for alias in v['aliases']:
                data['aliases'][alias] = k
            data['aliases'][k] = k
        self.ambiguities = data

        # Compound licenses
        # some compound licenses are incorrectly stated as
        # one, e.g. "GPL-2.0-with-classpath-exception" which
        # should be "GPL-2.0-only WITH
        # Classpath-exception-2.0". This file provides
        # translations for such
        compounds_file = self.config.get('compounds_file', LICENSE_COMPOUNDS_FILE)
        data = self.__read_json(compounds_file)
        self.license_db[COMPOUNDS_TAG] = data['compounds']
        for compound in data[COMPOUNDS_TAG]:
            licenses[compound['spdxid']] = compound
            for alias in compound[FLAME_ALIASES_TAG]:
                if alias in aliases:
                    raise FlameException(f'Alias "{alias}" -> {compound["spdxid"]} already defined as "{aliases[alias]}".')

                aliases[alias] = compound['spdxid']

            if COMPATIBILITY_AS_TAG in compound:
                compats[compound['spdxid']] = compound[COMPATIBILITY_AS_TAG]

        # Dual licenses
        # Read license with built-in dual feature, e.g
        # "GPL-2.0-or-later" which can be seen as a dual
        # license "GPL-2.0-only OR GPL-3.0-only"
        duals_file = self.config.get('duals_file', LICENSE_DUALS_FILE)
        data = self.__read_json(duals_file)
        for dual in data[DUAL_LICENSES_TAG]:
            duals[dual['spdxid']] = dual

        logging.debug(f'compounds_file: {compounds_file}')
        logging.debug(f'ambig_file: {ambig_file}')
        logging.debug(f'duals_file: {duals_file}')
        logging.debug(f'config: {self.config}')

        self.license_expression = license_expression.get_spdx_licensing()
        self.license_db[DUALS_TAG] = duals
        self.license_db[AMBIG_TAG] = self.ambiguities
        self.license_db[LICENSES_TAG] = licenses
        self.license_db[COMPATS_TAG] = compats
        self.license_db[FLAME_ALIASES_TAG] = aliases
        self.license_db[SCANCODE_KEYS_TAG] = scancode_keys
        self.license_db[LICENSE_OPERATORS_TAG] = self.__read_json(LICENSE_OPERATORS_FILE)['operators']

    def __identify_license(self, name):
        if name in self.license_db[LICENSES_TAG]:
            ret_name = name
            ret_id = 'direct'
        elif name in self.license_db[LICENSE_OPERATORS_TAG]:
            ret_name = self.license_db[LICENSE_OPERATORS_TAG][name]
            ret_id = 'operator'
        elif name in self.license_db[FLAME_ALIASES_TAG]:
            ret_name = self.license_db[FLAME_ALIASES_TAG][name]
            ret_id = 'alias'
        else:
            raise FlameException(f'Could not identify license from "{name}"')

        return {
            'queried_name': name,
            'name': ret_name,
            'identified_via': ret_id,
        }

    def __init_needles(self, needles, needle_tag, license_expression, allow_letter=False):
        if needle_tag not in self.needles_map:
            my_needles = []
            for needle in reversed(collections.OrderedDict(sorted(needles.items(), key=lambda x: len(x[0])))):

                if allow_letter:
                    reg_exp = r'( |>|\(|^|\)|\||/|[a-zA-Z0-9])%s( |<|$|\)|\||&|[a-zA-Z0-9])' % re.escape(needle)
                else:
                    reg_exp = r'( |>|\(|^|\)|\||/)%s( |<|$|\)|\||&)' % re.escape(needle)
                my_needles.append([re.compile(reg_exp), needle])
            self.needles_map[needle_tag] = my_needles

        return self.needles_map[needle_tag]

    def __update_license_expression_helper(self, needles, needle_tag, license_expression, allow_letter=False):
        replacements = []
        self.__init_needles(needles, needle_tag, license_expression, allow_letter)
        for c_needle in self.needles_map[needle_tag]:
            regexp = c_needle[0]
            needle = c_needle[1]
            if allow_letter:
                extra_add = ' '
            else:
                extra_add = ''

            if regexp.search(license_expression):
                replacement = needles[needle]
                extra_add = " "
                license_expression = regexp.sub(f'\\1{extra_add}{replacement}{extra_add}\\2', license_expression)

        fixed = re.sub(r'\s\s*', ' ', license_expression).strip()
        return {
            'license_expression': fixed,
            'identifications': replacements,
        }

    def __update_license_expression_helper_orig(self, needles, needle_tag, license_expression, allow_letter=False):
        replacements = []

        for needle in reversed(collections.OrderedDict(sorted(needles.items(), key=lambda x: len(x[0])))):
            if allow_letter:
                reg_exp = r'( |\(|^|\)|\||/)%s( |$|\)|\||&|[a-zA-Z])' % re.escape(needle)
                extra_add = ' '
            else:
                reg_exp = r'( |\(|^|\)|\||/)%s( |$|\)|\||&)' % re.escape(needle)
                extra_add = ''
            if re.search(reg_exp, license_expression):
                replacement = needles[needle]
                replacements.append({
                    'queried_name': needle,
                    'name': replacement,
                    'identified_via': needle_tag,
                })
                license_expression = re.sub(reg_exp, f'\\1 {replacement}{extra_add}\\2', license_expression)
        return {
            'license_expression': re.sub(r'\s\s*', ' ', license_expression).strip(),
            'identifications': replacements,
        }

    def __update_or_later(self, license_expression):
        updates = []
        new_expr = []
        for lic in re.split(LICENSE_SPLIT_RE, license_expression):
            if lic.strip() == '':
                continue
            trimmed_license = lic.strip().split()[0]
            if trimmed_license in self.license_db[DUALS_TAG]:
                newer_list = self.license_db[DUALS_TAG][trimmed_license][DUAL_NEWER_TAG]
                inner_new = []
                for newer in newer_list:
                    if len(inner_new) > 0:
                        inner_new.append('OR')
                    inner_new.append(lic.replace(trimmed_license, newer))
                inner_new_str = f' ( {" ".join(inner_new)} ) '
                updates.append({
                    'license-expression': lic,
                    'license': trimmed_license,
                    'updates': newer_list,
                    'updated-license': inner_new_str,
                })

                new_expr.append(inner_new_str)
            else:
                new_expr.append(lic)
        ret = ' '.join(new_expr)
        return {
            'input_license_expression': license_expression,
            'license_expression': ret,
            'updates': updates,
        }

    def expression_license(self, license_expression, validations=None, update_dual=True):
        r"""
        Return an object with information about the normalized license for the license given.

        :param license_expression: A license expression. E.g "BSD3" or "GPLv2+ || BSD3"
        :type license_expression: str
        :param validations: list of validations to perform
        :type license_expression: list
        :param update_dual: True to update implicit dual licenses
        :type update_dual: boolean
        :raise FlameException: if license_expression is not valid
        :return: a normalized license expression and meta information
        :rtype: list

        if you just want the normalized license, do:

           normalized_license = fl.expression_license('GNU & GPLv2+')['identified_license']


        A license gets normalized in te following steps/pipes:


        input/license ---> \

             +-------+
        ---> | alias | \
             +-------+

             +-------+
        ---> | ambig | \
             +-------+

             +----------+
        ---> | operator | \
             +----------+

             +-------+
        ---> | dual  | \
             +-------+

        ---> normalized

        Describing the steps/pipes:

        alias: normalizes license names like "BSD3" to SPDX identifiers like "BSD-3-Clause"

        ambig: identifies and stores ambiguities like "GNU", which is a project and not a license

        operator: normalizes operators like "||" to SPDX syntax like "OR"

        dual: normalizes implicit dual licenses like "GPL-2.0-or-later" to "GPL-2.0-only OR GPL-3.0-only" (note: updating dual license can be disabled)

        Example
        --------------------
        License: GNU & GPLv2+
          alias applied -----> GNU & GPL-2.0-or-later
          ambig applied -----> GNU & GPL-2.0-or-later  (ambiguity "GNU" discovered and saved)
          operator applied --> GNU AND GPL-2.0-or-later
          dual applied ------> GNU AND (GPL-2.0-only OR GPL-3.0-only)
        Normalized: GNU AND (GPL-2.0-only OR GPL-3.0-only)
        Ambiguities: GNU

        Example response:
        --------------------
        {
          "queried_license": "GNU & GPLv2+",
          "identified_license": "GNU AND (GPL-2.0-only OR GPL-3.0-only)",
          "identifications": [],
          "ambiguities": [
            {
              "license": "GNU & GPL-2.0-or-later",
              "ambigous_license": "GNU",
              "problem": "There a couple of licenses related to the GNU project. Without the name of the license and version number it is not possible to determine which of the versions is meant.",
              "description": "An ambiguity was identified in \"GNU & GPL-2.0-or-later\". The ambiguous license is \"GNU\". Problem: There a couple of licenses related to the GNU project. Without the name of the license and version number it is not possible to determine which of the versions is meant."
            }
          ],
          "updated_license": "GNU AND (GPL-2.0-only OR GPL-3.0-only)",
          "license_parsed": "GNU AND (GPL-2.0-only OR GPL-3.0-only)",
          "updates": [
            {
              "license-expression": "GPL-2.0-or-later",
              "license": "GPL-2.0-or-later",
              "updates": [
                "GPL-2.0-only",
                "GPL-3.0-only"
              ],
              "updated-license": " ( GPL-2.0-only OR GPL-3.0-only ) "
            }
          ]
        }

        :Example:
        >>> fl = FossLicenses()
        >>> expression = fl.expression_license('BSD3 & x11-keith-packard')
        >>> print(expression['identified_license'])
        BSD-3-Clause AND LicenseRef-flame-x11-keith-packard

        """
        if not isinstance(license_expression, str):
            raise FlameException(f'Wrong type ({type(license_expression)}) of input to the function expression_license. Only string is allowed. License expression: {license_expression}')

        # remove multiple blanks
        license_expression = re.sub(' [ ]*', ' ', license_expression)

        cache_key = f'{license_expression}__{validations}__{update_dual}'
        if cache_key in self.license_cache:
            return self.license_cache.get(cache_key)

        replacements = []

        ret = self.__update_license_expression_helper(self.license_db[FLAME_ALIASES_TAG],
                                                      'alias',
                                                      license_expression)
        replacements += ret['identifications']

        # manage ambiguities
        ambiguities = []
        aliases = self.license_db[AMBIG_TAG]['aliases']

        tmp_license_expression = ret['license_expression']
        for alias in reversed(collections.OrderedDict(sorted(aliases.items(), key=lambda x: len(x[0])))):

            needle = r'(?:\s+|^)%s(?:\s+|$)' % re.escape(alias)
            needle_tmp = self.__update_license_expression_helper(self.license_db[LICENSE_OPERATORS_TAG],
                                                                 "operator",
                                                                 needle,
                                                                 allow_letter=True)
            needle_fixed = needle_tmp["license_expression"]
            needle_fixed = needle.replace(" or ", " OR ")
            if re.search(needle_fixed, tmp_license_expression):
                real_lic = self.license_db[AMBIG_TAG]['aliases'][alias]
                if alias != real_lic:
                    about_license = f'An ambiguity was identified in "{ret["license_expression"]}". The ambiguous license is "{real_lic}", identified via "{alias}".'
                else:
                    about_license = f'An ambiguity was identified in "{ret["license_expression"]}". The ambiguous license is "{real_lic}".'
                problem = self.license_db[AMBIG_TAG]["ambiguities"][real_lic]["problem"]
                ambiguities.append({
                    'license': ret['license_expression'],
                    'ambigous_license': real_lic,
                    'problem': problem,
                    'description': f'{about_license} Problem: {problem}',
                })

        # operators
        ret = self.__update_license_expression_helper(self.license_db[LICENSE_OPERATORS_TAG],
                                                      'operator',
                                                      ret['license_expression'],
                                                      allow_letter=True)
        replacements += ret['identifications']

        update_problem = None
        updates = []
        if update_dual:
            # Manage dual licenses (such as GPL-2.0-or-later)
            updates_object = self.__update_or_later(ret['license_expression'])
            updates = updates_object['updates']
            try:
                updated_license = str(self.license_expression.parse(updates_object['license_expression']))
            except boolean.boolean.ParseError as e:
                update_problem = f'Could not parse \"{updates_object["license_expression"]}\". Exception: {e}'
                updated_license = updates_object['license_expression']
        else:
            try:
                updated_license = str(self.license_expression.parse(ret['license_expression']))
            except boolean.boolean.ParseError as e:
                update_problem = f'Could not parse "ret["license_expression"]". Exception: {e}'
                updated_license = ret['license_expression']

        license_parsed = updated_license

        self.__validate_license(validations, license_parsed)

        if update_problem:
            amb_text = ''
            if ambiguities:
                amb_text = f'Ambiguities were found: {ambiguities}.'

            parse_text = ''
            if update_problem:
                parse_text = f'Parsing failed: {update_problem}.'

            raise FlameException(f'Could not parse the license "{license_expression}". {amb_text} {parse_text}')

        ret = {
            'queried_license': license_expression,
            FLAME_IDENTIFIED_LICENSE_TAG: license_parsed,
            'identifications': replacements,
            'ambiguities': ambiguities,
            'updated_license': updated_license,
            'license_parsed': license_parsed,
            'updates': updates,
        }
        self.license_cache[cache_key] = ret
        return ret

    def licenses(self):
        """
        Returns all licenses supported by flame

        :Example:

        >>> fl = FossLicenses()
        >>> expression = fl.licenses()

        """
        return list(self.license_db[LICENSES_TAG].keys())

    def license_complete(self, name):
        """
        Return the corresponding license object

        :param name: spdx identifier, alias or scancode key2
        :type name: str
        :raise FlameException: if license_expression is not valid

        :Example:

        >>> fl = FossLicenses()
        >>> expression = fl.license_complete("MIT")

        """
        identified_name = self.__identify_license(name)['name']
        return self.license_db[LICENSES_TAG][identified_name]

    def license(self, name):
        """
        Return the normalized license name (SPDXID)

        :param name: spdx identifier, alias or scancode key2
        :type name: str
        :raise FlameException: if license_expression is not valid
        :return: SPDX identifier for the license 'name'
        :rtype: str

        :Example:

        >>> fl = FossLicenses()
        >>> license = fl.license('BSD3')
        >>> print(license['identified_element']['name'])
        BSD-3-Clause

        """

        identified_license = self.__identify_license(name)
        identified_name = identified_license[FLAME_NAME_TAG]
        if identified_license['identified_via'] == 'operator':
            return {
                IDENTIFIED_ELEMENT_TAG: identified_license,
                'operator': True,
            }
        else:
            return {
                IDENTIFIED_ELEMENT_TAG: identified_license,
                'license': self.license_db[LICENSES_TAG][identified_name],
            }

    def __OBSOLETE__license_spdxid(self, name):
        """
        name: spdx identifier, alias or scancode key2

        returns the corresponding spdxid
        """
        return self.license(name)['license']['spdxid']

    def __OBSOLETE__license_scancode_key(self, name):
        """
        name: spdx identifier, alias or scancode key2

        returns the corresponding scancode_key
        """
        return self.license(name)['license']['scancode_key']

    def compatibility_as_list(self) -> [str]:
        """Return a list of all the licenses missing in the OSADL matrix but having a known similar compatibility.

        :Example:

        >>> fl = FossLicenses()
        >>> compats = fl.compatibility_as_list()

        """
        # List all compatibility_as that exist
        licenses = self.license_db[LICENSES_TAG]
        return [{COMPATIBILITY_AS_TAG: licenses[x][COMPATIBILITY_AS_TAG], 'spdxid': licenses[x]['spdxid']} for x in licenses if COMPATIBILITY_AS_TAG in licenses[x]]

    def compound_list(self) -> [str]:
        """Return a list of all the compound licenses.

        :Example:

        >>> fl = FossLicenses()
        >>> compats = fl.compound_list()

        """
        # List all compounds
        return self.license_db[COMPOUNDS_TAG]

    def alias_list(self, alias_license: str = None) -> [str]:
        """Returns a list of all the aliases. Supplying alias_license
        will return a list of aliases beginning with alias_license

        :param str alias_license:  limit the list of alias to all matching alias_license

        :Example:

        >>> fl = FossLicenses()
        >>> aliases = fl.alias_list()

        """
        if alias_license:
            return {k: v for k, v in self.license_db[FLAME_ALIASES_TAG].items() if alias_license.lower() in v.lower()}
        # List all aliases that exist
        return self.license_db[FLAME_ALIASES_TAG]

    def ambiguities_list(self):
        """Returns a list of all the ambigious licenses.

        :Example:

        >>> fl = FossLicenses()
        >>> aliases = fl.ambiguities_list()

        """
        # List all aliases that exist
        return self.license_db[AMBIG_TAG]['ambiguities']

    def unknown_symbols(self, _license_expression, validations=None):
        """Returns a list of all all unknown license symbols for a license expression
        Note: developer function - not for "normal" users

        :Example:

        >>> fl = FossLicenses()
        >>> unknowns = fl.unknown_symbols(["MIT"])

        """
        license_expression = ' '.join(_license_expression)

        try:
            compat_license_expression = self.expression_license(license_expression, validations=validations, update_dual=False)
            fixed_license_expression = compat_license_expression['identified_license']
        except Exception:
            fixed_license_expression = license_expression
            ambig_aliases = {}
            for ambig in self.ambiguities_list():
                aliases = self.license_db[AMBIG_TAG]['ambiguities'][ambig]['aliases']
                for alias in aliases:
                    ambig_aliases[alias] = ambig

            for alias in reversed(collections.OrderedDict(sorted(ambig_aliases.items(), key=lambda x: len(x[0])))):
                if alias in fixed_license_expression:
                    fixed_license_expression = re.sub(re.escape(alias), ambig_aliases[alias], fixed_license_expression)
                    break

        compat_licenses = [x.strip() for x in re.split(LICENSE_SPLIT_RE_CLEAN, fixed_license_expression)]
        compat_licenses = [x for x in compat_licenses if x]
        unknown_symbols = set()
        for compat_license in compat_licenses:
            if compat_license not in self.known_symbols():
                unknown_symbols.add(compat_license)
        if len(unknown_symbols) != 0:
            raise FlameException('Unknown symbols identified.\n' + '\n'.join(list(unknown_symbols)))
        return 'OK', None

    def known_symbols(self):
        """Returns a list of all all known license symbols.

        :Example:

        >>> fl = FossLicenses()
        >>> symbols = fl.known_symbols()

        """
        _symbols = set()

        ambiguities = self.ambiguities_list()
        for ambig in ambiguities:
            _symbols.add(ambig)
            _symbols.update(set(ambiguities[ambig]['aliases']))

        licenses = self.license_db[LICENSES_TAG]
        for lic in licenses:
            _symbols.add(lic)
            _symbols.update(set(licenses[lic]['aliases']))

        operators = self.license_db[LICENSE_OPERATORS_TAG]
        for op in operators:
            _symbols.add(op)

        return list(_symbols)

    def aliases(self, license_name):
        """Returns a list of all the aliases for a license

        :param str license_name: Exact name (SPDXID) of the license

        :Example:

        >>> fl = FossLicenses()
        >>> aliases = fl.aliases("GPLv2+")

        """
        identified_name = self.__identify_license(license_name)[FLAME_NAME_TAG]
        return self.license_db[LICENSES_TAG][identified_name][FLAME_ALIASES_TAG]

    def operators(self):
        """
        Returns a list of all the supported (boolean) operators in license expressions.

        :Example:

        >>> fl = FossLicenses()
        >>> operators = fl.operators()

        """
        return self.license_db[LICENSE_OPERATORS_TAG]

    def __compatibility_as(self, license_name):
        # List compatibility_as for license
        identified = self.__identify_license(license_name)
        identified_name = identified[FLAME_NAME_TAG]

        if COMPATIBILITY_AS_TAG in self.license_db[LICENSES_TAG][identified_name]:
            compat = self.license_db[LICENSES_TAG][identified_name][COMPATIBILITY_AS_TAG]
            method = COMPATIBILITY_AS_TAG
        else:
            compat = identified_name
            method = 'direct'

        return {
            IDENTIFIED_ELEMENT_TAG: identified,
            FLAME_COMPATIBILITY_TAG: {
                'compat_as': compat,
                'queried_name': license_name,
                'identified_via': method,
            },
        }

    def simplify(self, expression):
        return self.license_expression.parse(' '.join(expression)).simplify()

    def expression_compatibility_as(self, license_expression, validations=None, update_dual=True):
        """Returns an object with information about the compatibility status for the license given.

        :param str license_expression: A license expression. E.g "BSD3" or "GPLv2+ || BSD3"

        :Example: supplying only one license, so look at [0]

        >>> fl = FossLicenses()
        >>> compat = fl.expression_compatibility_as('x11-keith-packard')
        >>> print(compat['compat_license'])
        HPND

        """

        cache_key = f'{license_expression}__{validations}__{update_dual}'
        if cache_key in self.compat_cache:
            return self.compat_cache.get(cache_key)

        expression_full = self.expression_license(license_expression, validations, update_dual)
        compats = []
        ret = self.__update_license_expression_helper(self.license_db[COMPATS_TAG],
                                                      'compat',
                                                      expression_full[FLAME_IDENTIFIED_LICENSE_TAG])
        ret['license_expression'] = re.sub(r'\s\s*', ' ', ret['license_expression']).strip()
        compats = ret['identifications']
        compat_license_expression = ret['license_expression']

        compat_licenses = [x.strip() for x in re.split(LICENSE_SPLIT_RE, compat_license_expression)]
        compat_licenses = [x for x in compat_licenses if x]
        compat_support = self.__validate_compatibilities_support(compat_licenses)

        self.__validate_license(validations, compat_license_expression)

        ret = {
            'ambiguities': expression_full['ambiguities'],
            'compatibilities': compats,
            'queried_license': license_expression,
            'identification': expression_full,
            FLAME_IDENTIFIED_LICENSE_TAG: expression_full[FLAME_IDENTIFIED_LICENSE_TAG],
            FLAME_COMPATIBLE_LICENSE_TAG: compat_license_expression,
            'compat_support': compat_support,
        }
        self.compat_cache[cache_key] = ret
        return ret

    def __validate_license(self, validations, license_expression):
        if validations:
            if Validation.SPDX in validations:
                self.__validate_license_spdx(license_expression)
            if Validation.SCANCODE in validations:
                self.__validate_license_scancode(license_expression)
            if Validation.RELAXED in validations:
                self.__validate_license_relaxed(license_expression)
            if Validation.OSADL in validations:
                compat_license_expression = self.expression_compatibility_as(license_expression)['compat_license']
                compat_licenses = [x.strip() for x in re.split('\\(|OR|AND|\\)', compat_license_expression)]
                compat_licenses = [x for x in compat_licenses if x]
                compat_support = self.__validate_compatibilities_support(compat_licenses)
                self.__validate_licenses_osadl(compat_support)

    def __validate_compatibilities_support(self, licenses):
        compat_support = {}
        compat_support['licenses'] = []
        all_supported = True
        for lic in licenses:
            support = self.__validate_compatibility_support(lic)
            compat_support['licenses'].append({
                'license': lic,
                'supported': support,
            })
            all_supported = all_supported and support
        compat_support['supported'] = all_supported

        return compat_support

    def __validate_compatibility_support(self, lic):
        if not self.supported_licenses:
            self.support_licenses = osadl_matrix.supported_licenses(self.license_matrix_file)
        return lic in self.support_licenses

    def __validate_license_spdx(self, expr):
        """
        """
        missing = []
        license_list = self.__license_list(expr)
        for _lic in license_list:
            lic = _lic.strip()
            spdx_license = lic in spdx_license_list.LICENSES
            spdx_exception = "exception" in lic.lower()
            if not (spdx_license or spdx_exception):
                missing.append(lic)

        if missing:
            raise FlameException(f'License validation of "{expr}" failed. Non SPDX identifiers: "{", ".join(missing)}"')

    def __validate_license_scancode(self, expr):
        """
        """
        expr_info = self.license_expression.validate(expr)

        if expr_info.errors:
            raise FlameException(f'License validation of "{expr}" failed. Errors: "{", ".join(expr_info.errors)}"')

    def __validate_license_relaxed(self, expr):
        """
        """
        license_list = self.__license_list(expr)
        for _lic in license_list:
            lic = _lic.strip()
            if " " in lic.strip():
                raise FlameException(f'Found license with multiple words "{lic}"')

    def __validate_licenses_osadl(self, compat_supported):
        """
        """
        if not compat_supported['supported']:
            raise FlameException('Not all licenses supported by OSADL\'s compatibility matrix',
                                 compat_supported)

    def __license_list(self, expr):
        SPDX_OPERATORS = ['AND', 'OR', 'WITH']
        return re.split(f'{"|".join(SPDX_OPERATORS)}', expr)

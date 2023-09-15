# SPDX-FileCopyrightText: 2023 Henrik Sandklef
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Simple class
"""
import collections
import glob
import json
import logging
import re
from pathlib import Path
import license_expression

from flame.config import LICENSE_DIR, LICENSE_OPERATORS_FILE, LICENSE_SCHEMA_FILE
from flame.exception import FlameException
from jsonschema import validate

json_schema = None

COMPATIBILITY_AS_TAG = 'compatibility_as'
COMPATIBILITY_TAG = 'compatibility'
IDENTIFIED_ELEMENT_TAG = 'identified_element'
SCANCODE_KEY_TAG = 'scancode_key'
SCANCODE_KEYS_TAG = 'scancode_keys'
LICENSES_TAG = 'licenses'
ALIASES_TAG = 'aliases'
COMPATS_TAG = 'compats'
NAME_TAG = 'name'

LICENSE_OPERATORS_TAG = 'license_operators'

class FossLicenses:

    def __init__(self, check=False, license_dir=LICENSE_DIR, logging_level=logging.INFO):
        logging.basicConfig(level=logging_level)
        self.license_dir = license_dir
        self.__init_license_db(check)

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
            data['license_text'] = lf.read()

        return data

    def __init_license_db(self, check=False):
        self.license_db = {}
        licenses = {}
        aliases = {}
        scancode_keys = {}
        compats = {}
        logging.debug(f'reading from: {self.license_dir}')
        for license_file in glob.glob(f'{self.license_dir}/*.json'):
            logging.debug(f' * {license_file}')
            data = self.__read_license_file(license_file, check)
            licenses[data['spdxid']] = data
            for alias in data[ALIASES_TAG]:
                if alias in aliases:
                    raise FlameException(f'Alias "{alias}" -> {data["spdxid"]} already defined as "{aliases[alias]}".')

                aliases[alias] = data['spdxid']
            if SCANCODE_KEY_TAG in data:
                scancode_keys[data[SCANCODE_KEY_TAG]] = data['spdxid']
            if COMPATIBILITY_AS_TAG in data:
                compats[data['spdxid']] = data[COMPATIBILITY_AS_TAG]

        self.license_expression = license_expression.get_spdx_licensing()
        self.license_db[LICENSES_TAG] = licenses
        self.license_db[COMPATS_TAG] = compats
        self.license_db[ALIASES_TAG] = aliases
        self.license_db[SCANCODE_KEYS_TAG] = scancode_keys
        self.license_db[LICENSE_OPERATORS_TAG] = self.__read_json(LICENSE_OPERATORS_FILE)['operators']

    def __identify_license(self, name):
        if name in self.license_db[LICENSES_TAG]:
            ret_name = name
            ret_id = 'direct'
        elif name in self.license_db[LICENSE_OPERATORS_TAG]:
            ret_name = self.license_db[LICENSE_OPERATORS_TAG][name]
            ret_id = 'operator'
        elif name in self.license_db[ALIASES_TAG]:
            ret_name = self.license_db[ALIASES_TAG][name]
            ret_id = 'alias'
        elif name in self.license_db[SCANCODE_KEYS_TAG]:
            ret_name = self.license_db[SCANCODE_KEYS_TAG][name]
            ret_id = 'scancode_key'
        else:
            raise FlameException(f'Could not identify license from "{name}"')

        return {
            'queried_name': name,
            'name': ret_name,
            'identified_via': ret_id,
        }

    def __update_license_expression_helper(self, needles, needle_tag, license_expression, allow_letter=False):
        replacements = []
        for needle in reversed(collections.OrderedDict(sorted(needles.items()))):
            if allow_letter:
                reg_exp = r'( |\(|^|\)|\|)%s( |$|\)|\||&|[a-zA-Z])' % re.escape(needle)
                extra_add = " "
            else:
                reg_exp = r'( |\(|^|\)|\|)%s( |$|\)|\||&)' % re.escape(needle)
                extra_add = ""

            if re.search(reg_exp, license_expression):
                replacement = needles[needle]
                replacements.append({
                    'queried_name': needle,
                    'name': replacement,
                    'identified_via': needle_tag,
                })
                license_expression = re.sub(reg_exp, f'\\1 {replacement}{extra_add}\\2', license_expression)
        return {
            "license_expression": re.sub(r'\s\s*', ' ', license_expression).strip(),
            "identifications": replacements
        }

    def expression_license(self, license_expression):
        """Returns an object with information about the normalized license for the license given.

        :param str license_expression: A license expression. E.g "BSD3" or "GPLv2+ || BSD3"
        """

        if not isinstance(license_expression, str):
            raise FlameException('Wrong type (type(license_expresssion)) of input to the function expression_license. Only string is allowed.')

        replacements = []

        ret = self.__update_license_expression_helper(self.license_db[ALIASES_TAG],
                                                      "alias",
                                                      license_expression)
        replacements += ret['identifications']

        ret = self.__update_license_expression_helper(self.license_db[SCANCODE_KEYS_TAG],
                                                      "scancode",
                                                      ret['license_expression'])
        replacements += ret['identifications']

        ret = self.__update_license_expression_helper(self.license_db[LICENSE_OPERATORS_TAG],
                                                      "operator",
                                                      ret['license_expression'],
                                                      allow_letter=True)
        replacements += ret['identifications']

        license_parsed = str(self.license_expression.parse(ret['license_expression']))

        return {
            'queried_license': license_expression,
            'identified_license': license_parsed,
            'identifications': replacements
        }

    def licenses(self):
        """
        Returns all licenses supported by flame
        """
        return list(self.license_db[LICENSES_TAG].keys())

    def license_complete(self, name):
        """
        name: spdx identifier of a license

        returns the corresponding license object
        """
        identified_name = self.__identify_license(name)['name']
        return self.license_db[LICENSES_TAG][identified_name]

    def license(self, name):
        """
        name: spdx identifier, alias or scancode key

        returns the normalized license name (SPDXID)
        """
        identified_license = self.__identify_license(name)
        identified_name = identified_license[NAME_TAG]
        if identified_license['identified_via'] == 'operator':
            return {
                IDENTIFIED_ELEMENT_TAG: identified_license,
                'operator': True
            }
        else:
            return {
                IDENTIFIED_ELEMENT_TAG: identified_license,
                'license': self.license_db[LICENSES_TAG][identified_name],
            }

    def __OBSOLETE__license_spdxid(self, name):
        """
        name: spdx identifier, alias or scancode key

        returns the corresponding spdxid
        """
        return self.license(name)['license']['spdxid']

    def __OBSOLETE__license_scancode_key(self, name):
        """
        name: spdx identifier, alias or scancode key

        returns the corresponding scancode_key
        """
        return self.license(name)['license']['scancode_key']

    def compatibility_as_list(self):
        # List all compatibility_as that exist
        licenses = self.license_db[LICENSES_TAG]
        return [{COMPATIBILITY_AS_TAG: licenses[x][COMPATIBILITY_AS_TAG], 'spdxid': licenses[x]['spdxid']} for x in licenses if COMPATIBILITY_AS_TAG in licenses[x]]

    def aliases_list(self, alias_license: str = None) -> [str]:
        """Returns a list of all the aliases. Supplying will alias_license
        will return a list of aliases beginning with alias_license

        :param str alias_license:  The person sending the message
        """
        if alias_license:
            return {k: v for k, v in self.license_db[ALIASES_TAG].items() if alias_license in v}
        # List all aliases that exist
        return self.license_db[ALIASES_TAG]

    def aliases(self, license_name):
        """Returns a list of all the aliases for a license

        :param str license_name: Exact name (SPDXID) of the license
        """
        identified_name = self.__identify_license(license_name)[NAME_TAG]
        return self.license_db[LICENSES_TAG][identified_name][ALIASES_TAG]

    def operators(self):
        """Returns a list of all the supported (boolean) operators in license expressions.
        """
        return self.license_db[LICENSE_OPERATORS_TAG]

    def __compatibility_as(self, license_name):
        # List compatibility_as for license
        identified = self.__identify_license(license_name)
        identified_name = identified[NAME_TAG]

        if COMPATIBILITY_AS_TAG in self.license_db[LICENSES_TAG][identified_name]:
            compat = self.license_db[LICENSES_TAG][identified_name][COMPATIBILITY_AS_TAG]
            method = COMPATIBILITY_AS_TAG
        else:
            compat = identified_name
            method = 'direct'

        return {
            IDENTIFIED_ELEMENT_TAG: identified,
            COMPATIBILITY_TAG: {
                'compat_as': compat,
                'queried_name': license_name,
                'identified_via': method
            }
        }

    def expression_compatibility_as(self, license_expression, validate_spdx=False, validate_relaxed=False):
        """Returns an object with information about the compatibility status for the license given.

        :param str license_expression: A license expression. E.g "BSD3" or "GPLv2+ || BSD3"
        """
        expression_full = self.expression_license(license_expression)
        compats = []
        ret = self.__update_license_expression_helper(self.license_db[COMPATS_TAG],
                                                      "compat",
                                                      expression_full['identified_license'])
        ret['license_expression'] = re.sub(r'\s\s*', ' ', ret['license_expression']).strip()
        compats = ret['identifications']
        compat_license_expression = ret['license_expression']

        if validate_spdx:
            self.__validate_license_spdx(compat_license_expression)
        elif validate_relaxed:
            self.__validate_license_relaxed(compat_license_expression)

        return {
            'compatibilities': compats,
            'queried_license': license_expression,
            'identifications': expression_full,
            'identified_license': expression_full['identified_license'],
            'compat_license': compat_license_expression
        }

    def __validate_license_spdx(self, expr):
        """
        """

        expr_info = self.license_expression.validate(expr)

        if expr_info.errors:
            raise FlameException(f'License validation failed. Errors: "{", ".join(expr_info.errors)}"')

    def __validate_license_relaxed(self, expr):
        """
        """
        SPDX_OPERATORS = ['AND', 'OR', 'WITH']
        license_list = re.split(f'{"|".join(SPDX_OPERATORS)}', expr)
        for _lic in license_list:
            lic = _lic.strip()
            if " " in lic.strip():
                raise FlameException(f'Found license with multiple words "{lic}"')

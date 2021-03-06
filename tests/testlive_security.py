#!/usr/bin/env python3
import os
import sys
import json
import unittest
from typing import Union

import urllib3  # type: ignore
import logging
import uuid
import warnings
import requests
import subprocess
from lxml.html import fromstring
from enum import Enum

try:
    from pymisp import PyMISP, MISPOrganisation, MISPUser, MISPRole
    from pymisp.exceptions import PyMISPError, NoKey, MISPServerError
except ImportError:
    if sys.version_info < (3, 6):
        print('This test suite requires Python 3.6+, breaking.')
        sys.exit(0)
    else:
        raise

# Load access information for env variables
url = "http://" + os.environ["HOST"]
key = os.environ["AUTH"]

# TODO?
urllib3.disable_warnings()
logging.disable(logging.CRITICAL)
logger = logging.getLogger('pymisp')


class ROLE(Enum):
    USER = 3
    ADMIN = 1
    ORG_ADMIN = 2


def check_response(response):
    if isinstance(response, dict) and "errors" in response:
        raise Exception(response["errors"])


def assert_error_response(response):
    if "errors" not in response:
        raise Exception(response)


def login(url: str, email: str, password: str) -> bool:
    session = requests.Session()

    r = session.get(url)
    r.raise_for_status()

    parsed = fromstring(r.text)
    form = parsed.forms[0]

    form_fields = form.fields

    login_form = {}
    for name in form_fields:
        login_form[name] = form_fields[name]
    login_form["data[User][email]"] = email
    login_form["data[User][password]"] = password

    r = session.post(url + form.action, login_form, allow_redirects=False)
    r.raise_for_status()
    if r.status_code == 302:
        r = session.get(r.headers['Location'].replace(":8080", ""), allow_redirects=False)  # TODO
        r.raise_for_status()

    r = session.get(url + "/users/view/me.json")
    try:
        r.raise_for_status()
    except requests.HTTPError:
        return False

    r = r.json()
    if email != r["User"]["email"]:
        raise Exception(r)  # logged in as different user
    return True


class MISPSetting:
    def __init__(self, connection: PyMISP, setting: str, value):
        self.__connection = connection
        self.__setting = setting
        self.__value = value

    def __enter__(self):
        original = self.__connection.get_server_setting(self.__setting)
        if "value" not in original:
            raise Exception(original)
        self.__original = original["value"]

        result = self.__connection.set_server_setting(self.__setting, self.__value, force=True)
        if "saved" not in result or not result["saved"]:
            raise Exception(result)

    def __exit__(self, exc_type, exc_val, exc_tb):
        result = self.__connection.set_server_setting(self.__setting, self.__original, force=True)
        if "saved" not in result or not result["saved"]:
            raise Exception(result)


class MISPComplexSetting:
    def __init__(self, new_setting: dict):
        self.new_setting = new_setting

    def __enter__(self):
        self.original = self.__run("modify", json.dumps(self.new_setting))

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.__run("replace", self.original)

    @staticmethod
    def __run(command: str, data: str) -> str:
        dir_path = os.path.dirname(os.path.realpath(__file__))
        r = subprocess.run(["php", dir_path + "/modify_config.php", command, data], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise Exception([r.returncode, r.stdout, r.stderr])
        return r.stdout.decode("utf-8")


def send(api: PyMISP, request_type: str, url: str, data=None, check_errors: bool = True) -> dict:
    if data is None:
        data = {}
    response = api._prepare_request(request_type, url, data=data)
    response = api._check_json_response(response)
    if check_errors:
        check_response(response)
    return response


def random() -> str:
    return str(uuid.uuid4()).split("-")[0]


class TestSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        warnings.simplefilter("ignore", ResourceWarning)

        # Connect as admin
        cls.admin_misp_connector = PyMISP(url, key)
        cls.admin_misp_connector.set_server_setting('debug', 1, force=True)
        cls.admin_misp_connector.global_pythonify = True
        # Check if admin is really site admin
        assert cls.admin_misp_connector._current_role.perm_site_admin

        # Create advanced authkey, so connector will work even after advanced keys are required
        cls.admin_advanced_authkey = cls.__create_advanced_authkey(cls, cls.admin_misp_connector._current_user.id)
        cls.admin_misp_connector.key = cls.admin_misp_connector.key + "," + cls.admin_advanced_authkey["authkey_raw"]

        # Creates an org
        organisation = MISPOrganisation()
        organisation.name = 'Test Org ' + random()  # make name always unique
        cls.test_org = cls.admin_misp_connector.add_organisation(organisation)
        check_response(cls.test_org)

        # Creates org admin
        org_admin = MISPUser()
        org_admin.email = 'testorgadmin@user' + random() + '.local'  # make name always unique
        org_admin.org_id = cls.test_org.id
        org_admin.role_id = 2  # Org admin role
        cls.test_org_admin = cls.admin_misp_connector.add_user(org_admin)
        check_response(cls.test_org_admin)

        # Creates advanced auth key for org admin
        cls.org_admin_advanced_authkey = cls.__create_advanced_authkey(cls, cls.test_org_admin.id)
        cls.org_admin_misp_connector = PyMISP(url, cls.test_org_admin.authkey + "," + cls.org_admin_advanced_authkey["authkey_raw"])
        cls.org_admin_misp_connector.global_pythonify = True

        # Creates an user
        cls.test_usr_password = str(uuid.uuid4())
        user = MISPUser()
        user.email = 'testusr@user' + random() + '.local'  # make name always unique
        user.org_id = cls.test_org.id
        user.role_id = 3  # User role
        user.password = cls.test_usr_password
        cls.test_usr = cls.admin_misp_connector.add_user(user)
        check_response(cls.test_usr)

        # Try to connect as user to check if everything works
        PyMISP(url, cls.test_usr.authkey)
        # Check if user can login with given password
        assert login(url, cls.test_usr.email, cls.test_usr_password)

    @classmethod
    def tearDownClass(cls):
        cls.admin_misp_connector.delete_user(cls.test_usr)
        cls.admin_misp_connector.delete_user(cls.test_org_admin)
        cls.admin_misp_connector.delete_organisation(cls.test_org)
        cls.__delete_advanced_authkey(cls, cls.admin_advanced_authkey["id"])
        cls.__delete_advanced_authkey(cls, cls.org_admin_advanced_authkey["id"])

    def setUp(self):
        # Do not show warning about not closed resources, because that something we want
        warnings.simplefilter("ignore", ResourceWarning)

    def test_not_logged_in(self):
        session = requests.Session()

        # Should redirect to login page
        for path in ("/", "/events/index", "/servers/index", "/users/checkIfLoggedIn"):
            r = session.get(url + path, allow_redirects=False)
            self.assertEqual(302, r.status_code, path)
            self.assertEqual(url + "/users/login", r.headers['Location'], path)

        # Should be accessible without login
        for path in ("/users/login", ):
            r = session.get(url + path, allow_redirects=False)
            self.assertEqual(200, r.status_code, path)

        with MISPSetting(self.admin_misp_connector, "Security.allow_self_registration", True):
            r = session.get(url + "/users/register", allow_redirects=False)
            self.assertEqual(200, r.status_code, path)

        with MISPSetting(self.admin_misp_connector, "Security.allow_self_registration", False):
            r = session.get(url + "/users/register", allow_redirects=False)
            self.assertEqual(302, r.status_code)
            self.assertEqual(url + "/users/login", r.headers['Location'])

    def test_empty_authkey(self):
        with self.assertRaises(NoKey):
            PyMISP(url, "")

    def test_invalid_length_authkey(self):
        with self.assertRaises(PyMISPError):
            PyMISP(url, "ahoj")

    def test_invalid_authkey(self):
        with self.assertRaises(PyMISPError):
            PyMISP(url, "pCZDbBr3wYPlY0DrlQzoD8EWrcClGc0Dqu2yMYyE")

    def test_invalid_authkey_start_end_correct(self):
        authkey = self.test_usr.authkey[0:4] + ("a" * 32) + self.test_usr.authkey[:-4]
        with self.assertRaises(PyMISPError):
            PyMISP(url, authkey)

    def test_no_auth_access(self):
        no_access_role = MISPRole()
        no_access_role.name = "No auth access"

        no_access_role = send(self.admin_misp_connector, "POST", 'admin/roles/add', data=no_access_role)
        self.assertFalse(no_access_role["Role"]["perm_auth"])
        no_access_role_id = no_access_role["Role"]["id"]

        # Change user role to no access role
        updated_user = self.admin_misp_connector.update_user({'role_id': no_access_role_id}, self.test_usr)
        check_response(updated_user)
        self.assertEqual(no_access_role_id, updated_user.role_id)

        with self.assertRaises(PyMISPError):
            PyMISP(url, self.test_usr.authkey)

        # Change user role back to origin one and try to connect
        updated_user = self.admin_misp_connector.update_user({'role_id': self.test_usr.role_id}, self.test_usr)
        check_response(updated_user)
        self.assertEqual(self.test_usr.role_id, updated_user.role_id)
        PyMISP(url, self.test_usr.authkey)

        # Delete test role
        self.admin_misp_connector._prepare_request('POST', f'admin/roles/delete/{no_access_role_id}')

    def test_assign_role_by_myself(self):
        logged_in = PyMISP(url, self.test_usr.authkey)
        logged_in.global_pythonify = True
        update_user = logged_in.update_user({'role_id': 1}, self.test_usr)
        # Check if role was not changed
        self.assertEqual(self.test_usr.role_id, update_user.role_id)

    def test_assign_site_admin_role_by_org_admin(self):
        with self.assertRaises(MISPServerError):
            self.org_admin_misp_connector.update_user({'role_id': 1}, self.test_usr)

    def test_user_must_change_password(self):
        updated_user = self.admin_misp_connector.update_user({'change_pw': 1}, self.test_usr)
        check_response(updated_user)
        self.assertEqual(updated_user.change_pw, "1")

        # Try to login, should still work because key is still valid
        PyMISP(url, self.test_usr.authkey)

        updated_user = self.admin_misp_connector.update_user({'change_pw': 0}, self.test_usr)
        check_response(updated_user)
        self.assertEqual(updated_user.change_pw, "0")

        # Try to login, should also still works
        PyMISP(url, self.test_usr.authkey)

    def test_user_must_change_password_by_myself(self):
        # Admin set that user must change password
        updated_user = self.admin_misp_connector.update_user({'change_pw': 1}, self.test_usr)
        check_response(updated_user)
        self.assertEqual(updated_user.change_pw, "1")

        # User try to change back trough API
        logged_in = PyMISP(url, self.test_usr.authkey)
        logged_in.update_user({'change_pw': 0}, self.test_usr)

        updated_user = self.admin_misp_connector.get_user(self.test_usr)
        # Should not be possible
        self.assertEqual(updated_user.change_pw, "1")

    def test_disabled_user(self):
        # Disable user
        updated_user = self.admin_misp_connector.update_user({'disabled': True}, self.test_usr)
        check_response(updated_user)
        self.assertTrue(updated_user.disabled)

        # Try to login
        self.assertFalse(login(url, self.test_usr.email, self.test_usr_password))

        # Enable user
        updated_user = self.admin_misp_connector.update_user({'disabled': False}, self.test_usr)
        check_response(updated_user)
        self.assertFalse(updated_user.disabled)

        # Try to login
        self.assertTrue(login(url, self.test_usr.email, self.test_usr_password))

    def test_disabled_user_api_access(self):
        # Disable user
        updated_user = self.admin_misp_connector.update_user({'disabled': True}, self.test_usr)
        check_response(updated_user)
        self.assertTrue(updated_user.disabled)

        # Try to login
        with self.assertRaises(PyMISPError):
            PyMISP(url, self.test_usr.authkey)

        # Enable user
        updated_user = self.admin_misp_connector.update_user({'disabled': False}, self.test_usr)
        check_response(updated_user)
        self.assertFalse(updated_user.disabled)

        # Try to login
        PyMISP(url, self.test_usr.authkey)

    def test_disabled_misp(self):
        with MISPSetting(self.admin_misp_connector, "MISP.live", False):
            self.assertFalse(login(url, self.test_usr.email, self.test_usr_password))

        # Check if user can login with given password
        self.assertTrue(login(url, self.test_usr.email, self.test_usr_password))

    def test_disabled_misp_api_access(self):
        with MISPSetting(self.admin_misp_connector, "MISP.live", False):
            # Try to login
            with self.assertRaises(PyMISPError):
                PyMISP(url, self.test_usr.authkey)

        # Try to login
        PyMISP(url, self.test_usr.authkey)

    def test_advanced_authkeys(self):
        with MISPSetting(self.admin_misp_connector, "Security.advanced_authkeys", True):
            # Create advanced authkey
            auth_key = self.__create_advanced_authkey(self.test_usr.id)

            # Try to login
            logged_in = PyMISP(url, auth_key["authkey_raw"])
            self.assertEqual(logged_in._current_user.id, self.test_usr.id)

            self.__delete_advanced_authkey(auth_key["id"])

    def test_advanced_authkeys_expired(self):
        with MISPSetting(self.admin_misp_connector, "Security.advanced_authkeys", True):
            # Create expired advanced authkey
            auth_key = self.__create_advanced_authkey(self.test_usr.id, {
                "expiration": "1990-01-05",
            })

            # Try to login
            with self.assertRaises(PyMISPError):
                PyMISP(url, auth_key["authkey_raw"])

            self.__delete_advanced_authkey(auth_key["id"])

    def test_advanced_authkeys_invalid_start_end_correct(self):
        with MISPSetting(self.admin_misp_connector, "Security.advanced_authkeys", True):
            # Create advanced authkey
            auth_key = self.__create_advanced_authkey(self.test_usr.id)

            # Try to login
            authkey = auth_key["authkey_raw"][0:4] + ("a" * 32) + auth_key["authkey_raw"][:-4]
            with self.assertRaises(PyMISPError):
                PyMISP(url, authkey)

            self.__delete_advanced_authkey(auth_key["id"])

    def test_advanced_authkeys_reset_own(self):
        with MISPSetting(self.admin_misp_connector, "Security.advanced_authkeys", True):
            # Create advanced authkey
            auth_key = self.__create_advanced_authkey(self.test_usr.id)

            # Try to login
            logged_in = PyMISP(url, auth_key["authkey_raw"])
            self.assertEqual(logged_in._current_user.id, self.test_usr.id)

            # Reset auth key
            new_auth_key = send(logged_in, "POST", "users/resetauthkey/me")
            new_auth_key = new_auth_key["message"].replace("Authkey updated: ", "")

            # Try to login with old key
            with self.assertRaises(PyMISPError):
                PyMISP(url, auth_key["authkey_raw"])

            # Try to login with new key
            logged_in = PyMISP(url, new_auth_key)
            self.assertEqual(logged_in._current_user.id, self.test_usr.id)

            self.__delete_advanced_authkey(auth_key["id"])
            # TODO: Delete new key

    def test_advanced_authkeys_reset_for_different_user(self):
        with MISPSetting(self.admin_misp_connector, "Security.advanced_authkeys", True):
            # Create advanced authkey
            auth_key = self.__create_advanced_authkey(self.test_usr.id)

            # Try to login
            logged_in = PyMISP(url, auth_key["authkey_raw"])
            self.assertEqual(logged_in._current_user.id, self.test_usr.id)

            # Reset auth key for different user
            new_auth_key = send(logged_in, "POST", "users/resetauthkey/1", check_errors=False)
            assert_error_response(new_auth_key)

            # Try to login again
            logged_in = PyMISP(url, auth_key["authkey_raw"])
            self.assertEqual(logged_in._current_user.id, self.test_usr.id)

            self.__delete_advanced_authkey(auth_key["id"])

    def test_advanced_authkeys_reset_org_admin(self):
        with MISPSetting(self.admin_misp_connector, "Security.advanced_authkeys", True):
            # Create advanced authkey
            auth_key = self.__create_advanced_authkey(self.test_usr.id)

            # Try to login
            logged_in = PyMISP(url, auth_key["authkey_raw"])
            self.assertEqual(logged_in._current_user.id, self.test_usr.id)

            # Reset auth key from org admin account
            new_auth_key = send(self.org_admin_misp_connector, "POST", f"users/resetauthkey/{self.test_usr.id}")
            new_auth_key = new_auth_key["message"].replace("Authkey updated: ", "")

            # Try to login with old key
            with self.assertRaises(PyMISPError):
                PyMISP(url, auth_key["authkey_raw"])

            # Try to login with new key
            logged_in = PyMISP(url, new_auth_key)
            self.assertEqual(logged_in._current_user.id, self.test_usr.id)

            self.__delete_advanced_authkey(auth_key["id"])
            # TODO: Delete new key

    def test_advanced_authkeys_view(self):
        with MISPSetting(self.admin_misp_connector, "Security.advanced_authkeys", True):
            auth_key = self.__create_advanced_authkey(self.test_usr.id)
            auth_key_id = auth_key["id"]
            auth_key = send(self.admin_misp_connector, "GET", f'authKeys/view/{auth_key_id}')
            self.__delete_advanced_authkey(auth_key_id)
            self.assertNotIn("authkey", auth_key["AuthKey"], "Response should not contain hashed authkey")

    def test_advanced_authkeys_index(self):
        with MISPSetting(self.admin_misp_connector, "Security.advanced_authkeys", True):
            auth_key_id = self.__create_advanced_authkey(self.test_usr.id)["id"]
            auth_keys = send(self.admin_misp_connector, "GET", 'authKeys/index/')
            self.__delete_advanced_authkey(auth_key_id)

            self.assertGreaterEqual(len(auth_keys), 1, "Response should contains at least one key")
            for auth_key in auth_keys:
                self.assertNotIn("authkey", auth_key["AuthKey"], "Response should not contain hashed authkey")

    def test_change_login(self):
        new_email = 'testusr@user' + random() + '.local'

        logged_in = PyMISP(url, self.test_usr.authkey)
        logged_in.global_pythonify = True

        # Change email
        updated_user = logged_in.update_user({'email': new_email}, self.test_usr)
        check_response(updated_user)
        self.assertEqual(new_email, updated_user.email)

        # Change email back
        updated_user = logged_in.update_user({'email': self.test_usr.email}, self.test_usr)
        check_response(updated_user)
        self.assertEqual(self.test_usr.email, updated_user.email)

    def test_change_login_disabled(self):
        with MISPSetting(self.admin_misp_connector, "MISP.disable_user_login_change", True):
            new_email = 'testusr@user' + random() + '.local'

            logged_in = PyMISP(url, self.test_usr.authkey)
            logged_in.global_pythonify = True

            # Try to change email
            updated_user = logged_in.update_user({'email': new_email}, self.test_usr)
            check_response(updated_user)

            # Change should be not successful
            self.assertEqual(self.test_usr.email, updated_user.email)

    def test_change_login_org_admin(self):
        # Try to change email as org admin
        new_email = 'testusr@user' + random() + '.local'
        updated_user = self.org_admin_misp_connector.update_user({'email': new_email}, self.test_usr)
        check_response(updated_user)

        # Change should be successful
        self.assertEqual(new_email, updated_user.email)

        # Change email back
        updated_user = self.org_admin_misp_connector.update_user({'email': self.test_usr.email}, self.test_usr)
        check_response(updated_user)
        self.assertEqual(self.test_usr.email, updated_user.email)

    def test_change_login_disabled_org_admin(self):
        with MISPSetting(self.admin_misp_connector, "MISP.disable_user_login_change", True):
            # Try to change email as org admin
            new_email = 'testusr@user' + random() + '.local'
            updated_user = self.org_admin_misp_connector.update_user({'email': new_email}, self.test_usr)
            assert_error_response(updated_user)

    def test_change_pw_disabled(self):
        with MISPSetting(self.admin_misp_connector, "MISP.disable_user_password_change", True):
            logged_in = PyMISP(url, self.test_usr.authkey)
            logged_in.global_pythonify = True
            logged_in.change_user_password(str(uuid.uuid4()))

        # Password should be still the same
        self.assertTrue(login(url, self.test_usr.email, self.test_usr_password))

    def test_change_pw_disabled_different_way(self):
        with MISPSetting(self.admin_misp_connector, "MISP.disable_user_password_change", True):
            logged_in = PyMISP(url, self.test_usr.authkey)
            logged_in.global_pythonify = True
            logged_in.update_user({"password": str(uuid.uuid4())}, self.test_usr.id)

        # Password should be still the same
        self.assertTrue(login(url, self.test_usr.email, self.test_usr_password))

    def test_change_pw_disabled_by_org_admin(self):
        with MISPSetting(self.admin_misp_connector, "MISP.disable_user_password_change", True):
            self.org_admin_misp_connector.update_user({"password": str(uuid.uuid4())}, self.test_usr.id)

        # Password should be still the same
        self.assertTrue(login(url, self.test_usr.email, self.test_usr_password))

    def test_add_user_by_org_admin(self):
        user = MISPUser()
        user.email = 'testusr@user' + random() + '.local'  # make name always unique
        user.org_id = self.test_org.id
        user.role_id = 3
        created_user = self.org_admin_misp_connector.add_user(user)
        check_response(created_user)

        deleted = self.org_admin_misp_connector.delete_user(created_user)
        check_response(deleted)

    def test_add_user_by_org_admin_to_different_org(self):
        user = MISPUser()
        user.email = 'testusr@user' + random() + '.local'  # make name always unique
        user.org_id = 1
        user.role_id = 3
        created_user = self.org_admin_misp_connector.add_user(user)
        check_response(created_user)

        # Org should be silently changed to correct org
        self.assertEqual(created_user.org_id, self.test_org_admin.org_id)

        deleted = self.org_admin_misp_connector.delete_user(created_user)
        check_response(deleted)

    def test_add_user_by_org_admin_disabled(self):
        with MISPSetting(self.admin_misp_connector, "MISP.disable_user_add", True):
            user = MISPUser()
            user.email = 'testusr@user' + random() + '.local'  # make name always unique
            user.org_id = self.test_org.id
            user.role_id = 3
            created_user = self.org_admin_misp_connector.add_user(user)
            assert_error_response(created_user)

    def test_change_user_org_by_org_admin_different_org(self):
        updated_user = self.org_admin_misp_connector.update_user({'org_id': 1}, self.test_usr)
        check_response(updated_user)

        # Org should be silently keep to correct org
        self.assertEqual(updated_user.org_id, self.test_usr.org_id)

    def test_change_user_org_by_myself(self):
        logged_in = PyMISP(url, self.test_usr.authkey)
        logged_in.global_pythonify = True
        updated_user = logged_in.update_user({'org_id': 1}, self.test_usr)

        # Org should be silently keep to correct org
        self.assertEqual(updated_user.org_id, self.test_usr.org_id)

    def test_shibb_existing_user(self):
        with MISPComplexSetting(self.__default_shibb_config()):
            session = requests.Session()
            session.headers["Email-Tag"] = self.test_usr.email
            session.headers["Federation-Tag"] = self.test_org.name
            session.headers["Group-Tag"] = "user"

            session.get(url, allow_redirects=False)
            r = session.get(url + "/users/view/me.json")
            r.raise_for_status()
            json_response = r.json()
            self.assertEqual(self.test_usr.email, json_response["User"]["email"])
            self.assertEqual(3, int(json_response["User"]["role_id"]))
            self.assertEqual(session.headers["Federation-Tag"], json_response["Organisation"]["name"])

    def test_shibb_new_user(self):
        with MISPComplexSetting(self.__default_shibb_config()):
            session = requests.Session()
            session.headers["Email-Tag"] = "external@user" + random() + ".local"
            session.headers["Federation-Tag"] = self.test_org.name
            session.headers["Group-Tag"] = "user"

            session.get(url, allow_redirects=False)
            r = session.get(url + "/users/view/me.json")
            r.raise_for_status()
            json_response = r.json()
            self.assertEqual(session.headers["Email-Tag"], json_response["User"]["email"])
            self.assertEqual(3, int(json_response["User"]["role_id"]))
            self.assertEqual(session.headers["Federation-Tag"], json_response["Organisation"]["name"])

            self.admin_misp_connector.delete_user(json_response["User"]["id"])

    def test_shibb_new_user_multiple_groups(self):
        with MISPComplexSetting(self.__default_shibb_config()):
            session = requests.Session()
            session.headers["Email-Tag"] = "external@user" + random() + ".local"
            session.headers["Federation-Tag"] = self.test_org.name
            session.headers["Group-Tag"] = "user,invalid,admin"

            session.get(url, allow_redirects=False)
            r = session.get(url + "/users/view/me.json")
            r.raise_for_status()
            json_response = r.json()
            self.assertEqual(session.headers["Email-Tag"], json_response["User"]["email"])
            self.assertEqual(1, int(json_response["User"]["role_id"]))
            self.assertEqual(session.headers["Federation-Tag"], json_response["Organisation"]["name"])

            self.admin_misp_connector.delete_user(json_response["User"]["id"])

    def test_shibb_new_user_non_exists_org(self):
        with MISPComplexSetting(self.__default_shibb_config()):
            session = requests.Session()
            session.headers["Email-Tag"] = "external@user" + random() + ".local"
            session.headers["Federation-Tag"] = "Non exists org " + random()
            session.headers["Group-Tag"] = "user"

            session.get(url, allow_redirects=False)
            r = session.get(url + "/users/view/me.json")
            r.raise_for_status()
            json_response = r.json()
            self.assertEqual(session.headers["Email-Tag"], json_response["User"]["email"])
            self.assertEqual(3, int(json_response["User"]["role_id"]))
            self.assertEqual(session.headers["Federation-Tag"], json_response["Organisation"]["name"])

            self.admin_misp_connector.delete_user(json_response["User"]["id"])
            self.admin_misp_connector.delete_organisation(json_response["User"]["org_id"])

    def test_shibb_new_user_org_uuid(self):
        with MISPComplexSetting(self.__default_shibb_config()):
            r = self.__shibb_login({
                "Email-Tag": "external@user" + random() + ".local",
                "Federation-Tag": self.test_org.uuid,
                "Group-Tag": "user",
            })

            r.raise_for_status()
            json_response = r.json()
            self.assertEqual(r.request.headers["Email-Tag"], json_response["User"]["email"])
            self.assertEqual(3, int(json_response["User"]["role_id"]))
            self.assertEqual(self.test_org.name, json_response["Organisation"]["name"])

            self.admin_misp_connector.delete_user(json_response["User"]["id"])
            self.admin_misp_connector.delete_organisation(json_response["User"]["org_id"])

    def test_shibb_new_user_non_exists_org_uuid(self):
        with MISPComplexSetting(self.__default_shibb_config()):
            r = self.__shibb_login({
                "Email-Tag": "external@user" + random() + ".local",
                "Federation-Tag": str(uuid.uuid4()),
                "Group-Tag": "user",
            })
            if r.status_code != 403:
                print(r.text)
                self.fail()

    def test_shibb_new_user_no_org_provided(self):
        with MISPComplexSetting(self.__default_shibb_config()):
            session = requests.Session()
            session.headers["Email-Tag"] = "external@user" + random() + ".local"
            session.headers["Group-Tag"] = "user"

            session.get(url, allow_redirects=False)
            r = session.get(url + "/users/view/me.json")
            r.raise_for_status()
            json_response = r.json()
            self.assertEqual(3, int(json_response["User"]["role_id"]))
            # Default org is used
            self.assertEqual(self.test_org.name, json_response["Organisation"]["name"])

            self.admin_misp_connector.delete_user(json_response["User"]["id"])

    def test_shibb_invalid_group(self):
        with MISPComplexSetting(self.__default_shibb_config()):
            session = requests.Session()
            session.headers["Email-Tag"] = "external@user" + random() + ".local"
            session.headers["Federation-Tag"] = self.test_org.name
            session.headers["Group-Tag"] = "invalid"

            session.get(url, allow_redirects=False)
            r = session.get(url + "/users/view/me.json")
            if r.status_code != 403:
                print(r.text)
                self.fail()

    def test_shibb_invalid_email(self):
        with MISPComplexSetting(self.__default_shibb_config()):
            session = requests.Session()
            session.headers["Email-Tag"] = "external.user" + random() + ".local"
            session.headers["Federation-Tag"] = self.test_org.name
            session.headers["Group-Tag"] = "user"

            session.get(url, allow_redirects=False)
            r = session.get(url + "/users/view/me.json")
            if r.status_code != 403:
                print(r.text)
                self.fail()

    def test_shibb_change_role(self):
        org_admin = self.__create_user(self.test_org.id, ROLE.ORG_ADMIN)

        with MISPComplexSetting(self.__default_shibb_config()):
            session = requests.Session()
            session.headers["Email-Tag"] = org_admin.email
            session.headers["Federation-Tag"] = self.test_org.name
            session.headers["Group-Tag"] = "user"

            session.get(url, allow_redirects=False)
            r = session.get(url + "/users/view/me.json")
            r.raise_for_status()
            json_response = r.json()
            # Change role back to user
            self.assertEqual(3, int(json_response["User"]["role_id"]))

        self.admin_misp_connector.delete_user(org_admin)

    def test_shibb_change_org(self):
        user = self.__create_user(self.test_org.id, ROLE.USER)

        with MISPComplexSetting(self.__default_shibb_config()):
            session = requests.Session()
            session.headers["Email-Tag"] = user.email
            session.headers["Federation-Tag"] = "Non exists org " + random()
            session.headers["Group-Tag"] = "user"

            session.get(url, allow_redirects=False)
            r = session.get(url + "/users/view/me.json")
            r.raise_for_status()
            json_response = r.json()
            # Change role back to user
            self.assertEqual(session.headers["Federation-Tag"], json_response["Organisation"]["name"])

            self.admin_misp_connector.delete_user(user)
            self.admin_misp_connector.delete_organisation(json_response["User"]["org_id"])

    def test_shibb_form_login(self):
        with MISPComplexSetting(self.__default_shibb_config()):
            # Form login should still works when no header provided
            self.assertTrue(login(url, self.test_usr.email, self.test_usr_password))

    def test_shibb_api_login(self):
        with MISPComplexSetting(self.__default_shibb_config()):
            PyMISP(url, self.test_usr.authkey)

    def test_shibb_enforced_existing_user(self):
        config = self.__default_shibb_config()
        config["Security"]["auth_enforced"] = True
        with MISPComplexSetting(config):
            r = self.__shibb_login({
                "Email-Tag": self.test_usr.email,
                "Federation-Tag": self.test_org.name,
                "Group-Tag": "user",
            })
            r.raise_for_status()
            json_response = r.json()
            self.assertEqual(self.test_usr.email, json_response["User"]["email"])
            self.assertEqual(3, int(json_response["User"]["role_id"]))
            self.assertEqual(self.test_org.name, json_response["Organisation"]["name"])

    def test_shibb_enforced_form_login(self):
        config = self.__default_shibb_config()
        config["Security"]["auth_enforced"] = True
        with MISPComplexSetting(config):
            # Form login should not work when shibb is enforced, because form doesn't exists
            with self.assertRaises(IndexError):
                login(url, self.test_usr.email, self.test_usr_password)

    def test_shibb_enforced_api_login(self):
        config = self.__default_shibb_config()
        config["Security"]["auth_enforced"] = True
        with MISPComplexSetting(config):
            PyMISP(url, self.test_usr.authkey)

    def __shibb_login(self, headers: dict) -> requests.Response:
        session = requests.Session()
        session.headers.update(headers)

        r = session.get(url, allow_redirects=False)
        if 500 <= r.status_code < 600:
            raise Exception(r)

        r = session.get(url + "/users/view/me.json")
        if 500 <= r.status_code < 600:
            raise Exception(r)

        return r

    def __create_user(self, org_id: int = None, role_id: Union[int, ROLE] = None) -> MISPUser:
        if isinstance(role_id, ROLE):
            role_id = role_id.value

        user = MISPUser()
        user.email = 'test@' + random() + '.local'  # make name always unique
        if org_id:
            user.org_id = org_id
        if role_id:
            user.role_id = role_id
        user = self.admin_misp_connector.add_user(user)
        check_response(user)
        if org_id:
            self.assertEqual(int(org_id), int(user.org_id))
        if role_id:
            self.assertEqual(int(role_id), int(user.role_id))
        return user

    def __create_advanced_authkey(self, user_id: int, data=None):
        return send(self.admin_misp_connector, "POST", f'authKeys/add/{user_id}', data=data)["AuthKey"]

    def __delete_advanced_authkey(self, key_id: int):
        return send(self.admin_misp_connector, "POST", f'authKeys/delete/{key_id}')

    def __default_shibb_config(self) -> dict:
        return {
            "ApacheShibbAuth": {
                "DefaultOrg": self.test_org.name,
                "UseDefaultOrg": False,
                "MailTag": "HTTP_EMAIL_TAG",
                "OrgTag": "HTTP_FEDERATION_TAG",
                "GroupTag": "HTTP_GROUP_TAG",
                "GroupSeparator": ",",
                "GroupRoleMatching": {
                    "admin": 1,
                    "user": 3,
                }
            },
            "Security":  {
                "auth": ["ShibbAuth.ApacheShibb"],
            }
        }


if __name__ == '__main__':
    unittest.main()

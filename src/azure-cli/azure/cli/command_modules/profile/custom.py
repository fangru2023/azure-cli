# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import os
import sys

from knack.log import get_logger
from knack.prompting import prompt_pass, NoTTYException
from knack.util import CLIError

from azure.cli.core._profile import Profile
from azure.cli.core.util import in_cloud_console

logger = get_logger(__name__)

cloud_resource_type_mappings = {
    "oss-rdbms": "ossrdbms_resource_id",
    "arm": "active_directory_resource_id",
    "aad-graph": "active_directory_graph_resource_id",
    "ms-graph": "microsoft_graph_resource_id",
    "batch": "batch_resource_id",
    "media": "media_resource_id",
    "data-lake": "active_directory_data_lake_resource_id"
}

_CLOUD_CONSOLE_LOGOUT_WARNING = ("Logout successful. Re-login to your initial Cloud Shell identity with"
                                 " 'az login --identity'. Login with a new identity with 'az login'.")
_CLOUD_CONSOLE_LOGIN_WARNING = ("Cloud Shell is automatically authenticated under the initial account signed-in with."
                                " Run 'az login' only if you need to use a different account")


LOGIN_ANNOUNCEMENT = (
    "[Announcements]\n"
    "With the new Azure CLI login experience, you can select the subscription you want to use more easily. "
    "Learn more about it and its configuration at https://go.microsoft.com/fwlink/?linkid=2271236\n\n"
    "If you encounter any problem, please open an issue at https://aka.ms/azclibug\n"
)

LOGIN_OUTPUT_WARNING = (
    "[WARNING] The login output has been updated. Please be aware that it no longer displays the full list of "
    "available subscriptions by default. To revert to the previous behavior, run "
    "`az config set core.login_experience_v2=off`.\n"
)


def list_subscriptions(cmd, all=False, refresh=False):  # pylint: disable=redefined-builtin
    """List the imported subscriptions."""
    from azure.cli.core.api import load_subscriptions

    subscriptions = load_subscriptions(cmd.cli_ctx, all_clouds=all, refresh=refresh)
    if not subscriptions:
        logger.warning('Please run "az login" to access your accounts.')
    for sub in subscriptions:
        sub['cloudName'] = sub.pop('environmentName', None)
    if not all:
        enabled_ones = [s for s in subscriptions if s.get('state') == 'Enabled']
        if len(enabled_ones) != len(subscriptions):
            logger.warning("A few accounts are skipped as they don't have 'Enabled' state. "
                           "Use '--all' to display them.")
            subscriptions = enabled_ones
    return subscriptions


def show_subscription(cmd, subscription=None):
    profile = Profile(cli_ctx=cmd.cli_ctx)
    return profile.get_subscription(subscription)


def get_access_token(cmd, subscription=None, resource=None, scopes=None, resource_type=None, tenant=None):
    """
    get AAD token to access to a specified resource.
    Use 'az cloud show' command for other Azure resources
    """
    if resource is None and resource_type:
        endpoints_attr_name = cloud_resource_type_mappings[resource_type]
        resource = getattr(cmd.cli_ctx.cloud.endpoints, endpoints_attr_name)

    profile = Profile(cli_ctx=cmd.cli_ctx)
    creds, subscription, tenant = profile.get_raw_token(subscription=subscription, resource=resource, scopes=scopes,
                                                        tenant=tenant)

    result = {
        'tokenType': creds[0],
        'accessToken': creds[1],
        'expires_on': creds[2]['expires_on'],
        'expiresOn': creds[2]['expiresOn'],
        'tenant': tenant
    }
    if subscription:
        result['subscription'] = subscription

    return result


def set_active_subscription(cmd, subscription):
    """Set the current subscription"""
    profile = Profile(cli_ctx=cmd.cli_ctx)
    if not id:
        raise CLIError('Please provide subscription id or unique name.')
    profile.set_active_subscription(subscription)


def account_clear(cmd):
    """Clear all stored subscriptions. To clear individual, use 'logout'"""
    _remove_adal_token_cache()

    if in_cloud_console():
        logger.warning(_CLOUD_CONSOLE_LOGOUT_WARNING)
    profile = Profile(cli_ctx=cmd.cli_ctx)
    profile.logout_all()


# pylint: disable=inconsistent-return-statements, too-many-branches
def login(cmd, username=None, password=None, service_principal=None, tenant=None, allow_no_subscriptions=False,
          identity=False, use_device_code=False, use_cert_sn_issuer=None, scopes=None, client_assertion=None):
    """Log in to access Azure subscriptions"""

    # quick argument usage check
    if any([password, service_principal, tenant]) and identity:
        raise CLIError("usage error: '--identity' is not applicable with other arguments")
    if any([password, service_principal, username, identity]) and use_device_code:
        raise CLIError("usage error: '--use-device-code' is not applicable with other arguments")
    if use_cert_sn_issuer and not service_principal:
        raise CLIError("usage error: '--use-sn-issuer' is only applicable with a service principal")
    if service_principal and not username:
        raise CLIError('usage error: --service-principal --username NAME --password SECRET --tenant TENANT')

    interactive = False

    profile = Profile(cli_ctx=cmd.cli_ctx)

    if identity:
        if in_cloud_console():
            return profile.login_in_cloud_shell()
        return profile.login_with_managed_identity(username, allow_no_subscriptions)
    if in_cloud_console():  # tell users they might not need login
        logger.warning(_CLOUD_CONSOLE_LOGIN_WARNING)

    if username:
        if not (password or client_assertion):
            try:
                password = prompt_pass('Password: ')
            except NoTTYException:
                raise CLIError('Please specify both username and password in non-interactive mode.')
    else:
        interactive = True

    if service_principal:
        from azure.cli.core.auth.identity import ServicePrincipalAuth
        password = ServicePrincipalAuth.build_credential(password, client_assertion, use_cert_sn_issuer)

    select_subscription = interactive and sys.stdin.isatty() and sys.stdout.isatty() and \
        cmd.cli_ctx.config.getboolean('core', 'login_experience_v2', fallback=True)

    subscriptions = profile.login(
        interactive,
        username,
        password,
        service_principal,
        tenant,
        scopes=scopes,
        use_device_code=use_device_code,
        allow_no_subscriptions=allow_no_subscriptions,
        use_cert_sn_issuer=use_cert_sn_issuer,
        show_progress=select_subscription)

    # No JSON output if interactive account selection is used
    if select_subscription:
        from ._subscription_selector import SubscriptionSelector
        from azure.cli.core._profile import _SUBSCRIPTION_ID

        selected = SubscriptionSelector(subscriptions)()
        profile.set_active_subscription(selected[_SUBSCRIPTION_ID])

        print(LOGIN_ANNOUNCEMENT)
        logger.warning(LOGIN_OUTPUT_WARNING)
        return

    all_subscriptions = list(subscriptions)
    for sub in all_subscriptions:
        sub['cloudName'] = sub.pop('environmentName', None)
    return all_subscriptions


def logout(cmd, username=None):
    """Log out to remove access to Azure subscriptions"""
    _remove_adal_token_cache()

    if in_cloud_console():
        logger.warning(_CLOUD_CONSOLE_LOGOUT_WARNING)

    profile = Profile(cli_ctx=cmd.cli_ctx)
    if not username:
        username = profile.get_current_account_user()
    profile.logout(username)


def check_cli(cmd):
    from azure.cli.core.file_util import (
        create_invoker_and_load_cmds_and_args, get_all_help)

    exceptions = {}

    print('Running CLI self-test.\n')

    print('Loading all commands and arguments...')
    try:
        create_invoker_and_load_cmds_and_args(cmd.cli_ctx)
        print('Commands loaded OK.\n')
    except Exception as ex:  # pylint: disable=broad-except
        exceptions['load_commands'] = ex
        logger.error('Error occurred loading commands!\n')
        raise ex

    print('Retrieving all help...')
    try:
        get_all_help(cmd.cli_ctx, skip=False)
        print('Help loaded OK.\n')
    except Exception as ex:  # pylint: disable=broad-except
        exceptions['load_help'] = ex
        logger.error('Error occurred loading help!\n')
        raise ex

    if not exceptions:
        print('CLI self-test completed: OK')
    else:
        raise CLIError(exceptions)


def _remove_adal_token_cache():
    """Remove ADAL token cache file ~/.azure/accessTokens.json, as it is no longer needed by MSAL-based Azure CLI.
    """
    from azure.cli.core._environment import get_config_dir
    adal_token_cache = os.path.join(get_config_dir(), 'accessTokens.json')
    try:
        os.remove(adal_token_cache)
        return True  # Deleted
    except FileNotFoundError:
        return False  # Not exist

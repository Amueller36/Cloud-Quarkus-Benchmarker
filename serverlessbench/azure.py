import json
import os
import shutil
import sys
import platform
import time
import datetime
from tzlocal import get_localzone

from typing import Dict
from serverlessbench.logger import LoggingBase
from serverlessbench.utils import execute, compute_directory_hash, find_cache, update_cache, load_config


class Azure(LoggingBase):
    def __init__(self):
        super().__init__()
        self.mvwn = 'mvnw.cmd' if platform.system() == 'Windows' else 'mvnw'

    def deploy(self, root_path, config, deployments, benchmark_name, benchmark, function_name, native, update):
        self.__precheck()
        mvnw_path = os.path.join(root_path, self.mvwn)
        timeout = benchmark['timeout']
        storage = benchmark.get('storage', False)
        submodule_path = os.path.join(root_path, "benchmarks", benchmark_name)

        region = config['providers']['azure']['region']
        subscription = config['providers']['azure'].get('subscription')
        resource_group = config['providers']['azure'].get('resource-group')
        app_service_plan_name = config['providers']['azure'].get('app-service-plan-name')

        src_hash = compute_directory_hash(os.path.join(submodule_path, 'src'))
        target_hash = compute_directory_hash(os.path.join(submodule_path, 'target')) if os.path.exists(
            os.path.join(submodule_path, 'target')) else None
        cache = find_cache('azure', benchmark_name, native)

        if not cache or cache['src_hash'] != src_hash or cache['target_hash'] != target_hash:
            self.build(mvnw_path, benchmark_name, submodule_path, function_name, native)
            target_hash = compute_directory_hash(os.path.join(submodule_path, 'target'))
            update_cache('azure', benchmark_name, native, src_hash, target_hash)
        else:
            self.logging.warning(f"Skipping build for {'azure-native' if native else 'azure'}. No changes detected.")

        url, account_name, account_key, app_insights_instrumentation_key = self.deploy_function(function_name, mvnw_path,
                                                                                                timeout, submodule_path, region,
                                                                                                native, storage,
                                                                                                subscription,
                                                                                                resource_group,
                                                                                                app_service_plan_name,
                                                                                                update)

        deployments['azure']["native" if native else "jvm"][benchmark_name] = {
            'function_name': function_name,
            'url': url,
            'account_name': account_name,
            'account_key': account_key,
            'bucket': function_name if storage else None,
            'app_insights_instrumentation_key': app_insights_instrumentation_key
        }

        return deployments, config

    def build(self, mvnw_path, benchmark_name, submodule_path, function_name, native):
        profile = 'azure-native' if native else 'azure'
        self.logging.info(f'Building benchmark "{benchmark_name}" with profile "{profile}".')
        execute([mvnw_path, 'clean', 'package', '-Dquarkus.azure-functions.app-name=' + function_name,
                 '-Dquarkus.http.root-path=/api', '-P', profile], "Error while building project.",
                self.logging, cwd=submodule_path)
        self.logging.debug(f'Benchmark "{benchmark_name}" successfully built with profile "{profile}".')

    def deploy_function(self, function_name, mvnw_path, timeout, submodule_path, region, native, storage,
                        subscription=None, resource_group=None, app_service_plan_name=None, update=False):

        deployCmd = [mvnw_path, 'quarkus:deploy',
                     '-Dquarkus.azure-functions.app-name=' + function_name,
                     '-Dquarkus.azure-functions.app-settings.STORAGE_BUCKET=' + function_name,
                     '-Dquarkus.azure-functions.app-settings.AzureFunctionsJobHost__functionTimeout=' + self._convert_seconds(timeout),
                     '-Dquarkus.azure-functions.region=' + region]

        appSettingsCmd = ['az', 'functionapp', 'config', 'appsettings', 'list', '--name', function_name]

        if subscription is not None:
            deployCmd.append('-Dquarkus.azure-functions.subscription-id=' + subscription)
            appSettingsCmd.extend(['--subscription', subscription])
        if resource_group is not None:
            deployCmd.append('-Dquarkus.azure-functions.resource-group=' + resource_group)
            appSettingsCmd.extend(['--resource-group', resource_group])
        else:
            appSettingsCmd.extend(['--resource-group', 'quarkus'])
        if app_service_plan_name is not None:
            deployCmd.append('-Dquarkus.azure-functions.app-service-plan-name=' + app_service_plan_name)

        if native:
            deployCmd.extend(['-Dquarkus.azure-functions.app-settings.FUNCTIONS_WORKER_RUNTIME=custom'])

        deployCmd.extend(['-P', 'azure'])

        execute(deployCmd, f'Error while building and deploying function "{function_name}" to Azure.',
                self.logging,
                cwd=submodule_path)
        self.logging.debug(f'Function "{function_name}" deployed to Azure Functions.')

        appSettings = json.loads(
            execute(appSettingsCmd, "Error while fetching app settings for function", self.logging))

        account_name, account_key, app_insights_instrumentation_key = self._extract_appsettings(appSettings)

        if storage and not update:
            benchmark_name = os.path.basename(submodule_path)
            benchmarks_data_path = os.path.abspath(
                os.path.join(submodule_path, "../../benchmarks-data", benchmark_name))
            self.create_storage_container(account_name, function_name)
            if os.path.exists(benchmarks_data_path) and os.path.isdir(benchmarks_data_path):
                self.upload_folder_to_storage_container(account_name, function_name, benchmarks_data_path, "input")

        return f"https://{function_name}.azurewebsites.net/api", account_name, account_key, app_insights_instrumentation_key

    def create_storage_container(self, account_name, container_name):
        self.logging.info(
            f'Creating Azure Blob Storage container {container_name} in Storage account "{account_name}".')
        execute(['az', 'storage', 'container', 'create', '--account-name', account_name, '--name', container_name],
                "Error while creating Azure Blob Storage container.",
                self.logging)

    def upload_folder_to_storage_container(self, account_name, container_name, folder_path, folder_name):
        self.logging.info(
            f'Uploading folder "{folder_path}" to Azure Blob Storage container "{container_name}" in Storage account "{account_name}".')
        execute(['az', 'storage', 'blob', 'upload-batch', '--account-name', account_name,
                 '--destination', f'{container_name}/{folder_name}', '--source', folder_path],
                "Error while uploading folder to Azure Blob Storage container.", self.logging)

    def _extract_appsettings(self, data: dict):
        account_name = None
        account_key = None
        app_insights_instrumentation_key = None

        def extract_value_from_connection_string(connection_string, key):
            parts = connection_string.split(';')
            for part in parts:
                if part.startswith(key + '='):
                    return part.split('=', 1)[1]
            return None

        for item in data:
            if item['name'] == 'APPINSIGHTS_INSTRUMENTATIONKEY':
                app_insights_instrumentation_key = item['value']
            elif item['name'] == 'AzureWebJobsStorage':
                connection_string = item['value']
                account_name = extract_value_from_connection_string(connection_string, 'AccountName')
                account_key = extract_value_from_connection_string(connection_string, 'AccountKey')

        return account_name, account_key, app_insights_instrumentation_key

    def enforce_cold_start(self, function_name):
        config = load_config()
        resource_group = config['providers']['azure'].get('resource_group')

        env_vars = json.loads(execute([
            'az', 'functionapp', 'config', 'appsettings', 'list',
            '--name', function_name,
            '--resource-group', resource_group if resource_group else 'quarkus'],
            "Error while getting Azure function configuration.", self.logging))

        env_vars_dict = {env_var['name']: env_var['value'] for env_var in env_vars}
        current_value = int(env_vars_dict.get('cold_start_var', '0'))
        new_value = current_value + 1

        execute(['az', 'functionapp', 'config', 'appsettings', 'set',
                 '--name', function_name,
                 '--resource-group', resource_group if resource_group else 'quarkus',
                 '--settings', 'cold_start_var='+str(new_value)],
                f"Error while updating Azure function environment variable for cold start.", self.logging)
        time.sleep(20)

    def delete(self, function_name, account_name, resource_group=None):
        self.logging.info(f'Deleting Azure Function "{function_name}".')
        execute(['az', 'functionapp', 'delete', '--resource-group', resource_group if resource_group else 'quarkus',
                 '--name', function_name],
                "Error while deleting Azure Function.",
                self.logging)

        execute(['az', 'storage', 'account', 'delete', '--name', account_name, '--yes'],
                "Error while deleting Azure Storage Account.",
                self.logging)

        execute(['az', 'monitor', 'app-insights', 'component', 'delete', '--app', function_name,
                 '--resource-group', resource_group if resource_group else 'quarkus'],
                "Error while deleting Application Insights.",
                self.logging)

    def __precheck(self):
        if shutil.which('az') is None:
            self.logging.error('azure CLI is not installed. Please install it before proceeding.')
            sys.exit(1)

        execute(['az', 'account', 'show'], 'Error while fetching Azure account information.', self.logging,
                disableCmdLog=True)

        execute(['az', 'config', 'set', 'extension.use_dynamic_install=yes_without_prompt'],
                'Error while setting Azure CLI configuration.', self.logging, disableCmdLog=True)

    def _convert_seconds(self, seconds):
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        seconds = seconds % 60

        # Format the time as hh:mm:ss
        return f"{int(hours):02}:{int(minutes):02}:{int(seconds):02}"

    def enrich_metrics(self, function_name: str, start_time: int, end_time: int, requests: Dict[str, dict]):
        max_retries = 10  # Maximum number of retries
        retry_interval = 10  # Interval between retries in seconds

        time.sleep(100)
        resource_group = load_config()['providers']['azure'].get('resource_group')
        resource_group = resource_group if resource_group else 'quarkus'

        app_id_query = execute(['az', 'monitor', 'app-insights', 'component', 'show',
                                '--app', function_name,
                                '--resource-group', resource_group],
                               "Error while fetching App Insights application ID.", self.logging)
        application_id = json.loads(app_id_query)["appId"]

        start_time_str = datetime.datetime.fromtimestamp(start_time).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )
        end_time_str = datetime.datetime.fromtimestamp(end_time + 1).strftime("%Y-%m-%d %H:%M:%S")
        timezone_str = datetime.datetime.now(get_localzone()).strftime("%z")

        query = (
            "requests | project timestamp, operation_Name, success, "
            "resultCode, duration, cloud_RoleName, "
            "invocationId=customDimensions['InvocationId'], "
            "functionTime=customDimensions['FunctionExecutionTimeMs']"
        )
        invocations_processed: set[str] = set()
        invocations_to_process = set(requests.keys())

        retries = 0
        while retries < max_retries and len(invocations_processed) < len(requests.keys()):
            self.logging.info("Azure: Running App Insights query.")
            ret = execute(['az', 'monitor', 'app-insights', 'query',
                           '--app', application_id,
                           '--analytics-query', f"{query}",
                           '--start-time', start_time_str, timezone_str,
                           '--end-time', end_time_str, timezone_str],
                          "Error while fetching App Insights metrics.", self.logging)
            ret = json.loads(ret)
            ret = ret["tables"][0]

            for request in ret["rows"]:
                invocation_id = request[-2]
                if invocation_id not in requests:
                    continue
                func_exec_time = request[-1]
                invocations_processed.add(invocation_id)
                requests[invocation_id]["provider_time"] = float(func_exec_time) / 1000

            self.logging.info(
                f"Found time metrics for {len(invocations_processed)} out of {len(requests.keys())} invocations."
            )
            if len(invocations_processed) < len(requests.keys()):
                self.logging.info(f"Missing the requests: {invocations_to_process - invocations_processed}")
                time.sleep(retry_interval)
                retries += 1

        if len(invocations_processed) < len(requests.keys()):
            self.logging.warning(
                f"Failed to find metrics for {len(invocations_to_process - invocations_processed)} invocations after {max_retries} retries."
            )

        return requests

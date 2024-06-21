import os
import shutil
import sys
import platform
import json
import time
import math
from typing import Dict, Union, cast
from serverlessbench.logger import LoggingBase
from serverlessbench.utils import execute, compute_directory_hash, find_cache, update_cache, load_config


class AWS(LoggingBase):
    def __init__(self):
        super().__init__()
        self.mvwn = 'mvnw.cmd' if platform.system() == 'Windows' else 'mvnw'

    def deploy(self, root_path, config, deployments, benchmark_name, benchmark, function_name, native, update):
        self.__precheck()
        memory = benchmark['memory'][0] if isinstance(benchmark['memory'], list) else benchmark['memory']
        timeout = benchmark['timeout']
        storage = benchmark.get('storage', False)
        submodule_path = os.path.join(root_path, "benchmarks", benchmark_name)

        aws_access_key_id = config['providers']['aws'].get('aws_access_key_id')
        aws_secret_access_key = config['providers']['aws'].get('aws_secret_access_key')

        if config['providers']['aws'].get('lambda-role') is None:
            self.logging.info("No Lambda role found in config. Creating a new one.")
            config['providers']['aws']['role'] = self.create_lambda_role()

        if storage and (aws_access_key_id is None or aws_secret_access_key is None):
            self.logging.info("No AWS credentials found in config. Please provide them.")
            sys.exit(1)

        role = config['providers']['aws']['lambda-role']
        region = config['providers']['aws']['region']

        src_hash = compute_directory_hash(os.path.join(submodule_path, 'src'))
        target_hash = compute_directory_hash(os.path.join(submodule_path, 'target')) if os.path.exists(
            os.path.join(submodule_path, 'target')) else None
        cache = find_cache('aws', benchmark_name, native)

        if not cache or cache['src_hash'] != src_hash or cache['target_hash'] != target_hash:
            self.build(root_path, benchmark_name, submodule_path, native)
            target_hash = compute_directory_hash(os.path.join(submodule_path, 'target'))
            update_cache('aws', benchmark_name, native, src_hash, target_hash)
        else:
            self.logging.warning(f"Skipping build for {'aws-native' if native else 'aws'}. No changes detected.")

        if update:
            self.update_lambda_code(function_name, submodule_path, region)
            return deployments, config

        url = self._deploy_lambda(function_name, memory, timeout, submodule_path, region, role,
                                  native, storage, aws_access_key_id, aws_secret_access_key)

        deployments['aws']["native" if native else "jvm"][benchmark_name] = {
            'function_name': function_name,
            'url': url,
            'bucket': function_name if storage else None
        }

        return deployments, config

    def build(self, root_path, benchmark_name, submodule_path, native):
        mvnw_path = os.path.join(root_path, self.mvwn)
        profile = 'aws-native' if native else 'aws'
        self.logging.info(f'Building benchmark "{benchmark_name}" with profile "{profile}".')
        execute([mvnw_path, 'clean', 'package', '-P', profile], "Error while building project.",
                self.logging, cwd=submodule_path)
        self.logging.debug(f'Benchmark "{benchmark_name}" successfully built with profile "{profile}".')

    def _deploy_lambda(self, function_name, memory, timeout, submodule_path, region, role, native, storage,
                       aws_access_key_id, aws_secret_access_key):
        if storage:
            benchmark_name = os.path.basename(submodule_path)
            benchmarks_data_path = os.path.abspath(
                os.path.join(submodule_path, "../../benchmarks-data", benchmark_name))
            self.create_s3_bucket(function_name, region, role)
            if os.path.exists(benchmarks_data_path) and os.path.isdir(benchmarks_data_path):
                self.upload_folder_to_s3(function_name, benchmarks_data_path, "input")

        function_path = os.path.join(submodule_path, 'target', 'function.zip')
        command = [
            'aws', 'lambda', 'create-function',
            '--function-name', function_name,
            '--runtime', 'provided.al2023' if native else 'java21',
            '--role', role,
            '--ephemeral-storage', '{"Size": 1024}'
                                   '--handler',
            'not.used.in.provided.runtime' if native else 'io.quarkus.amazon.lambda.runtime.QuarkusStreamHandler::handleRequest',
            '--zip-file', f'fileb://{function_path}',
            '--memory-size', str(memory),
            '--timeout', str(timeout),
            '--region', region
        ]

        environment_vars = {
            "DISABLE_SIGNAL_HANDLERS": "true" if native else None,
            "STORAGE_BUCKET": function_name if storage else None,
            "S3_ENDPOINT": f"https://s3.{region}.amazonaws.com" if storage else None,
            "S3_ACCESS_KEY_ID": aws_access_key_id if storage else None,
            "S3_SECRET_ACCESS_KEY": aws_secret_access_key if storage else None,
        }
        environment_vars = {k: v for k, v in environment_vars.items() if v is not None}
        environment_vars_str = 'Variables={' + ','.join(f'{k}={v}' for k, v in environment_vars.items()) + '}'
        command.extend(['--environment', environment_vars_str])

        self.logging.info(f'Deploying "{function_name}" to AWS Lambda.')
        execute(command, "Error while deploying Lambda function.", self.logging)
        self.logging.debug(f'Function "{function_name}" deployed to AWS Lambda.')

        self._add_permission(function_name, region)

        return self._create_function_url(function_name, region)

    def create_s3_bucket(self, bucket_name, region, role):
        self.logging.info(f'Creating S3 bucket "{bucket_name}" in region "{region}".')
        execute(
            ['aws', 's3api', 'create-bucket',
             '--bucket', bucket_name,
             '--region', region,
             '--create-bucket-configuration',
             f'LocationConstraint={region}'],
            "Error while creating S3 bucket.",
            self.logging
        )
        execute(
            ['aws', 's3api', 'put-object', '--bucket', bucket_name, '--key', 'input/'],
            "Error while creating input folder in S3 bucket.",
            self.logging
        )
        execute(
            ['aws', 's3api', 'put-object', '--bucket', bucket_name, '--key', 'output/'],
            "Error while creating output folder in S3 bucket.",
            self.logging
        )
        self.logging.info(f'S3 bucket "{bucket_name}" created.')

    def upload_folder_to_s3(self, bucket_name, folder_path, s3_prefix):
        self.logging.info(f'Uploading contents of {folder_path} to s3://{bucket_name}/{s3_prefix}/.')
        execute(
            ['aws', 's3', 'cp', '--recursive', folder_path, f's3://{bucket_name}/{s3_prefix}/'],
            "Error while uploading folder to S3.",
            self.logging
        )
        self.logging.debug(f'Contents of {folder_path} uploaded to s3://{bucket_name}/{s3_prefix}/.')

    def update_lambda_code(self, function_name, submodule_path, region):
        function_path = os.path.join(submodule_path, 'target', 'function.zip')

        self.logging.info(f'Updating "{function_name}" on AWS Lambda.')
        execute([
            'aws', 'lambda', 'update-function-code',
            '--function-name', function_name,
            '--zip-file', f'fileb://{function_path}',
            '--region', region
        ], "Error while updating Lambda function.", self.logging)
        self.logging.debug(f'Function "{function_name}" updated on AWS Lambda.')

    def update_lambda_memory(self, function_name, memory):
        config = load_config()
        region = config['providers']['aws'].get('region')

        self.logging.info(f'Updating "{function_name}" memory on AWS Lambda.')
        execute([
            'aws', 'lambda', 'update-function-configuration',
            '--function-name', function_name,
            '--memory-size', str(memory),
            '--region', region
        ], "Error while updating Lambda memory.", self.logging)
        execute(['aws', 'lambda', 'wait', 'function-updated-v2',
                 '--function-name', function_name,
                 '--region', region], "Error while waiting for Lambda update.", self.logging, disableCmdLog=True)

    def enforce_cold_start(self, function_name):
        config = load_config()
        region = config['providers']['aws'].get('region')

        env_vars = execute([
            'aws', 'lambda', 'get-function-configuration',
            '--function-name', function_name,
            '--query', 'Environment.Variables',
            '--output', 'json',
            '--region', region], "Error while getting Lambda function configuration.", self.logging, disableCmdLog=True)

        if env_vars is None or env_vars == '':
            env_vars = {}
        else:
            env_vars = json.loads(env_vars)

        current_value = int(env_vars.get('cold_start_var', '0'))
        new_value = current_value + 1
        env_vars['cold_start_var'] = str(new_value)
        env_vars_str = ','.join([f'{k}={v}' for k, v in env_vars.items()])

        execute([
            'aws', 'lambda', 'update-function-configuration',
            '--function-name', function_name,
            '--environment', 'Variables={' + env_vars_str + '}',
            '--region', region
        ], f"Error while updating Lambda environment variable.", self.logging)
        execute(['aws', 'lambda', 'wait', 'function-updated-v2',
                 '--function-name', function_name,
                 '--region', region], "Error while waiting for Lambda update.", self.logging, disableCmdLog=True)

    def _add_permission(self, function_name, region):
        self.logging.debug(f'Adding permission to allow public access to "{function_name}".')
        execute([
            'aws', 'lambda', 'add-permission',
            '--function-name', function_name,
            '--statement-id', 'FunctionURLAllowPublicAccess',
            '--action', 'lambda:InvokeFunctionUrl',
            '--principal', '*',
            '--function-url-auth-type', 'NONE',
            '--region', region
        ], "Error while adding permission to Lambda function.", self.logging)
        self.logging.debug(f'Permission added to "{function_name}" to allow public access.')

    def _create_function_url(self, function_name, region):
        self.logging.debug(f'Creating Function URL for "{function_name}".')
        function_url = execute([
            'aws', 'lambda', 'create-function-url-config',
            '--auth-type', 'NONE',
            '--function-name', function_name,
            '--region', region,
            '--query', 'FunctionUrl', '--output', 'text'
        ], "Error while creating Function URL for Lambda function.", self.logging)

        function_url = function_url.strip()
        function_url = function_url[:-1] if function_url.endswith('/') else function_url
        self.logging.debug(f'Function URL ({function_url}) created for "{function_name}".')

        return function_url

    def delete(self, function_name, bucket, region):
        self.logging.info(f'Deleting Lambda function "{function_name}".')
        execute(['aws', 'lambda', 'delete-function', '--function-name', function_name, '--region', region],
                "Error while deleting Lambda function.", self.logging)

        if bucket:
            self.logging.info(f'Deleting S3 bucket "{bucket}".')
            execute(['aws', 's3', 'rb', f's3://{bucket}', '--force'],
                    "Error while deleting S3 bucket.", self.logging)

        self.logging.info(f'Deleting CloudWatch log group for Lambda function "{function_name}".')
        log_group_name = f"/aws/lambda/{function_name}"
        execute(['aws', 'logs', 'delete-log-group', '--log-group-name', log_group_name, '--region', region],
                "Error while deleting CloudWatch log group.", self.logging)

    def create_lambda_role(self) -> str:
        role_arn = execute([
            'aws', 'iam', 'create-role',
            '--role-name', 'lambda-ex',
            '--assume-role-policy-document',
            '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}',
            '--query', 'Role.Arn', '--output', 'text'
        ], "Error while creating Lambda role.", self.logging)

        execute([
            'aws', 'iam', 'attach-role-policy',
            '--role-name', 'lambda-ex',
            '--policy-arn', 'arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
        ], "Error while attaching Lambda role policy [AWSLambdaBasicExecutionRole].", self.logging)

        execute([
            'aws', 'iam', 'attach-role-policy',
            '--role-name', 'lambda-ex',
            '--policy-arn', 'arn:aws:iam::aws:policy/AmazonS3FullAccess'
        ], "Error while attaching Lambda role policy [AmazonS3FullAccess].", self.logging)

        self.logging.info(f"Lambda role {role_arn.stdout.strip()} created.")
        return role_arn.stdout.strip()

    def __precheck(self):
        if shutil.which('aws') is None:
            self.logging.error('aws CLI is not installed. Please install it before proceeding.')
            sys.exit(1)

    def start_aws_query(self, function_name: str, start_time: int, end_time: int, region: str):
        query_response = execute([
            "aws", "logs", "start-query",
            "--log-group-name", f"/aws/lambda/{function_name}",
            "--query-string", "filter @message like /REPORT/",
            "--start-time", str(math.floor(start_time)),
            "--end-time", str(math.ceil(end_time + 1)),
            "--limit", "10000",
            "--region", region,
            "--output", "json"
        ], "Error while starting AWS query.", self.logging)
        return json.loads(query_response)["queryId"]

    def get_aws_query_results(self, query_id: str, region: str):
        response = None
        while response is None or json.loads(response)["status"] == "Running":
            self.logging.info("Waiting for AWS query to complete ...")
            time.sleep(1)
            response = execute(["aws", "logs", "get-query-results",
                                "--query-id", query_id,
                                "--region", region,
                                "--output", "json"], "Error while getting AWS query results.", self.logging)
        return json.loads(response)["results"]

    def process_query_results(self, results, requests):
        results_processed = 0
        requests_ids = set(requests.keys())

        for val in results:
            for result_part in val:
                if result_part["field"] == "@message":
                    request_id = self.parse_aws_report(result_part["value"], requests)
                    if request_id in requests:
                        results_processed += 1
                        requests_ids.remove(request_id)

        return results_processed

    def parse_aws_report(self, log: str, requests: Union[dict, Dict[str, dict]]) -> str:
        aws_vals = {}
        for line in log.split("\t"):
            if not line.isspace():
                split = line.split(":")
                aws_vals[split[0]] = split[1].split()[0]
        if "START RequestId" in aws_vals:
            request_id = aws_vals["START RequestId"]
        else:
            request_id = aws_vals["REPORT RequestId"]
        if isinstance(requests, dict):
            output = requests
        else:
            if request_id not in requests:
                return request_id
            output = requests[request_id]
        output[request_id]["provider_time"] = float(aws_vals["Duration"]) / 1000

        return request_id

    def enrich_metrics(self, function_name: str, start_time: int, end_time: int, requests: Dict[str, dict]):
        region = load_config()['providers']['aws'].get('region')
        time.sleep(100)

        results_count = len(requests.keys())
        results_processed = 0

        while results_processed < results_count:
            query_id = self.start_aws_query(function_name, start_time, end_time, region)
            results = self.get_aws_query_results(query_id, region)
            results_processed = self.process_query_results(results, requests)

            self.logging.info(
                f"Received {len(results)} entries, found results for {results_processed} out of {results_count} invocations")

            if results_processed < results_count:
                self.logging.info("Re-querying AWS logs for missing invocations...")
                time.sleep(10)

        return requests

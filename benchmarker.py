#!/usr/bin/env python3
import json
import os
import time
import uuid
from enum import Enum
from typing import Any, Dict, Optional, List, Tuple
import random
import click
import urllib3
from tabulate import tabulate

from serverlessbench.aws import AWS
from serverlessbench.azure import Azure
from serverlessbench.gcp import GCP
from serverlessbench.knative import Knative
from serverlessbench.logger import LoggingBase
from serverlessbench.utils import load_config, load_deployments, get_benchmark_names, get_runtime_names


class LoadProfile(Enum):
    COLD = "cold"
    WARM = "warm"
    BURST = "burst"


class FunctionInvocationResult:
    def __init__(self, request_id: str, client_begin: int, client_end: int, client_time: float,
                 response_body: dict | str | None):
        self.request_id = request_id
        self.provider_time = None  # Will be set later
        self.client_begin = client_begin
        self.client_end = client_end
        self.client_time = client_time
        self.response_body = response_body

    def toJSON(self):
        return json.dumps(
            self,
            default=lambda o: o.__dict__,
            sort_keys=True,
            indent=4)


class Benchmarker(LoggingBase):
    def __init__(self):
        super().__init__()

        self.aws = AWS()
        self.azure = Azure()
        self.gcp = GCP()
        self.knative = Knative()

        self.root_path = os.getcwd()
        self.config = load_config()
        self.deployments = load_deployments()
        self.benchmarks_info = self.__load_benchmarks_info()
        self.http = urllib3.PoolManager(timeout=urllib3.Timeout(total=120.0))  # 2 minute timeout

    def __load_benchmarks_info(self) -> Dict[str, Dict[str, Any]]:
        """Load and return benchmark data from the config file."""
        benchmarks_data = {}

        for benchmark in self.config['benchmarks']:
            benchmark_name = benchmark.get('name')
            if benchmark_name:
                benchmarks_data[benchmark_name] = {
                    'endpoint': benchmark.get('endpoint'),
                    'request': benchmark.get('request'),
                    'memory': benchmark.get('memory'),
                }

        return benchmarks_data

    def __get_provider_time_and_update_results(self, provider: str, function_name: str, start_time: int, end_time: int,
                                               results: Dict[str, dict]):
        """Get provider time and update the results dictionary."""

        if provider == 'gcp':
            return self.gcp.enrich_metrics(function_name=function_name, start_time=start_time, end_time=end_time,
                                           requests=results)
        elif provider == 'aws':
            return self.aws.enrich_metrics(function_name, start_time, end_time, results)
        elif provider == 'azure':
            return self.azure.enrich_metrics(function_name, start_time, end_time, results)
        elif provider == 'knative':
            # TODO: Implement AWS specific provider time logic here
            return results

    def __get_benchmark_data(self, providers, benchmark_names) -> Dict[str, Dict[str, Any]]:
        """
        Filter benchmark data by provider and/or benchmark name.
        If no provider or benchmark name is specified, all benchmarks for all providers will be returned.
        """
        results = {}

        # Check if deployments exist for the specified providers in the deployments.json file
        if providers:
            providers_with_existing_deployments = set(self.deployments.keys())
            providers_with_no_deployments = set(providers) - providers_with_existing_deployments

            if providers_with_no_deployments:
                self.logging.warning(
                    f"No Deployments found for Provider(s) ({', '.join(providers_with_no_deployments)}) in the deployments.json file. "
                    f"Did you deploy the benchmarks?"
                )

        for prov, runtimes in self.deployments.items():
            if providers and prov not in providers:
                continue

            for runtime, benchmarks in runtimes.items():
                for bench_name, details in benchmarks.items():
                    if benchmark_names and bench_name not in benchmark_names:
                        continue

                    function_name = details['function_name']
                    base_url = details['url']

                    if bench_name in self.benchmarks_info:
                        benchmark_info = self.benchmarks_info[bench_name]
                        http_method = benchmark_info['request'].get('method')
                        endpoint = benchmark_info.get('endpoint')
                        body = benchmark_info['request'].get('body')
                        benchmark_url = f"{base_url}{endpoint}"

                        # Populate the results dictionary
                        results.setdefault(prov, {}).setdefault(runtime, {}).setdefault(bench_name, {}).update({
                            "function_name": function_name,
                            "benchmark_url": benchmark_url,
                            "method": http_method,
                            "body": body,
                            "memory": benchmark_info.get('memory'),
                        })
        if not results:
            self.logging.error(
                f"No Deployment infos found for the specified provider/s \"{providers}\" and/or benchmark names \"{benchmark_names}\". Did you deploy the benchmarks? And do they exist in the deployments.json file exist?")
            exit(-1)
        return results

    def enforce_cold_start(self, provider: str, function_name: str, native: bool):
        """Perform specific logic to enforce cold start for the given provider."""

        if provider == 'gcp':
            self.gcp.enforce_cold_start(function_name=function_name, native=native)
        elif provider == 'aws':
            self.aws.enforce_cold_start(function_name)
        elif provider == 'azure':
            self.azure.enforce_cold_start(function_name)
        elif provider == 'knative':
            pass  # Implement Knative specific cold start logic here
        else:
            self.logging.error(
                f"Unsupported provider: \"{provider}\". Supported providers are 'aws', 'azure', 'gcp', or 'knative'.")

    def invoke_function(self, provider: str, url: str, method: str,
                        request_body: Optional[dict]) -> FunctionInvocationResult:
        """Execute a benchmark request and measure its response time using urllib3."""

        response_body = None
        client_side_response_time = None
        request_id = None
        headers = {'Content-Type': 'application/json'}
        if provider == "knative":
            request_id = str(uuid.uuid4())
            headers[
                'x-client-trace-id'] = request_id  # Knative requires to set a request id manually, for the other providers the request id is extracted from the response headers
        body_data = json.dumps(request_body) if request_body else None

        try:
            start_time = int(time.time() * 1_000_000)
            response = self.http.request(
                method.upper(),
                url,
                body=body_data,
                headers=headers,
            )
            end_time = int(time.time() * 1_000_000)
            client_side_response_time = (end_time - start_time) / 1_000_000
            request_id = self._get_request_id(headers=response.headers, provider=provider)

            response_body = response.data.decode('utf-8')
            response_dict = json.loads(response_body) if response_body else {}

        except urllib3.exceptions.HTTPError as e:
            self.logging.error(f"HTTP error occurred for {url}: {e}")
            exit(-1)
        except urllib3.exceptions.RequestError as e:
            self.logging.error(f"Request error occurred for {url}: {e}")
            exit(-1)
        except json.JSONDecodeError as e:
            self.logging.error(f"Error decoding JSON response for {url}: {e}")
            response_dict = response_body
        except Exception as e:
            self.logging.error(f"An unknown error occurred for {url}: {e}")
            exit(-1)
        return FunctionInvocationResult(request_id=request_id,
                                        client_begin=start_time,
                                        client_end=end_time,
                                        client_time=client_side_response_time,
                                        response_body=response_dict)

    def _get_request_id(self, headers: Dict[str, str], provider: str) -> str:
        if provider == 'aws':
            return headers.get('x-amzn-RequestId')
        elif provider == 'azure':
            return headers.get('X-Azure-Functions-InvocationId')
        elif provider == 'gcp':
            return headers.get('X-Cloud-Trace-Context').split(';')[0]
        elif provider == 'knative':
            return headers.get('x-client-trace-id')

    def start_run(self,
                  load_profile: LoadProfile,
                  providers: Optional[List[str]] = None,
                  benchmark_names: Optional[List[str]] = None,
                  runtimes_to_include: Optional[List[str]] = None,
                  repetitions: int = 10,
                  ):
        """Execute benchmarks and save results."""

        # 1. Fetch benchmark urls/methods and request body
        # 2. Enforce cold start if specified

        self.logging.info(
            f"Starting benchmarks. Providers: {providers}, Benchmarks: {benchmark_names}, Load Profile: {load_profile.value}, Repetitions: {repetitions}")

        benchmark_data = self.__get_benchmark_data(providers, benchmark_names)

        for prov, runtimes in benchmark_data.items():
            for runtime, benchmarks in runtimes.items():
                if runtime not in runtimes_to_include:
                    self.logging.info(f"Skipping Benchmarks for runtime: {runtime}")
                    continue
                for bench_name, bench_details in benchmarks.items():

                    memory_sizes = [None] if prov == 'azure' else bench_details['memory']

                    for memory in memory_sizes:
                        __begin = time.time()
                        function_name = bench_details['function_name']
                        benchmark_url = bench_details['benchmark_url']
                        http_method = bench_details['method']
                        request_body = bench_details['body']

                        benchmark_results = {}

                        self.logging.info(
                            f"Invoking benchmark {bench_name} for {prov.upper()} provider, {runtime.upper()} runtime. "
                            f"Load Profile: {load_profile.value} , Memory Size: {memory}MB")
                        # Set Memory size for the function
                        if memory:
                            self._set_memory_for_function(provider=prov, function_name=function_name, memory=memory,
                                                          native=(runtime == 'native'))

                        if load_profile == LoadProfile.COLD:
                            # Call a Benchmark x amount of times while enforcing a cold start after each request
                            cold_start_counter = 0
                            while cold_start_counter < repetitions:

                                self.enforce_cold_start(provider=prov, function_name=function_name,
                                                        native=(runtime == 'native'))
                                result = self.invoke_function(provider=prov, url=benchmark_url, method=http_method,
                                                              request_body=request_body)
                                if not result.response_body.get('is_cold'):
                                    self.logging.error(
                                        f"Expected a cold start, but it was not detected. Benchmark: {bench_name}, "
                                        f"Provider: {prov}, Runtime: {runtime}")
                                    time.sleep(5)
                                    continue

                                cold_start_counter += 1
                                benchmark_results.setdefault(result.request_id,
                                                             json.loads(
                                                                 FunctionInvocationResult(request_id=result.request_id,
                                                                                          client_begin=result.client_begin,
                                                                                          client_end=result.client_end,
                                                                                          client_time=result.client_time,
                                                                                          response_body=result.response_body).toJSON()))
                        if load_profile == LoadProfile.WARM:
                            # TODO: Implement logic for warm start here

                            pass

                        if load_profile == LoadProfile.BURST:
                            # TODO: Implement logic for burst start here
                            pass

                        # Save benchmark results to a file
                        results_dir = os.path.join(self.root_path, 'benchmark_results', prov, runtime, bench_name)
                        os.makedirs(results_dir, exist_ok=True)

                        benchmark_results = self.__get_provider_time_and_update_results(prov,
                                                                                        function_name,
                                                                                        __begin - 1,
                                                                                        time.time() + 1,
                                                                                        benchmark_results)

                        with open(os.path.join(results_dir,
                                               f'{load_profile.name}_{repetitions}_{memory if memory else "default"}.json'),
                                  'w') as f:
                            json.dump(benchmark_results, f, indent=4)

        self.logging.info("Benchmark invocation completed and results saved.")

    def _set_memory_for_function(self, provider: str, function_name: str, memory: int, native: bool):
        if provider == 'gcp':
            self.gcp.set_memory_for_function(function_name=function_name, memory=memory, native=native)
        elif provider == 'aws':
            self.aws.update_lambda_memory(function_name, memory)
        elif provider == 'knative':
            # TODO: Implement AWS specific memory setting logic here
            pass

    def log_benchmark_results(self, results, intended_cold_start: bool):
        results_table_data = []
        headers = ["Provider", "Benchmark Name", "Client-SideResponse Time (s)", "Provider Start", "Provider End",
                   "Was it a Cold Start?", "Intended Cold Start?"]
        for prov, runtimes in results.items():
            for runtime, benchmarks in runtimes.items():
                for bench_name, details in benchmarks.items():
                    results_table_data.append([
                        prov, bench_name, details['client_side_response_time'], details['response_data']['begin'],
                        details['response_data']['end'], details['response_data'].get('is_cold', 'N/A'),
                        intended_cold_start])

        self.logging.info("Benchmark results:")
        self.logging.info("\n" + tabulate(results_table_data, headers=headers, tablefmt="pretty"))


@click.command()
@click.option('-p', '--providers',
              help='Specify the provider to run benchmarks for. If not specified, all providers will be benchmarked.',
              default=['gcp', 'aws', 'azure', 'knative'],
              multiple=True, type=click.Choice(['aws', 'azure', 'gcp', 'knative']))
@click.option('-b', '--benchmarks', multiple=True,
              default=get_benchmark_names(),
              help='Specify which benchmarks to run.', type=click.Choice(get_benchmark_names()))
@click.option('-ru', '--runtimes', multiple=True,
              default=get_runtime_names(),
              help='Specify which runtimes should be ran', type=click.Choice(get_runtime_names()))
@click.option('-l', '--load-profile',
              required=True,
              help='Specify a Load Profile for the Benchmark',
              type=click.Choice([profile.value for profile in LoadProfile])
              )
@click.option('-r', '--repetitions',
              required=True,
              help='Specify how often to invoke the benchmark.',
              type=click.INT
              )
def main(providers: Optional[List[str] | Tuple[str]], benchmarks: Optional[List[str] | Tuple[str]],
         runtimes: Optional[List[str] | Tuple[str]],
         load_profile: LoadProfile, repetitions: int):
    """CLI entry point for running benchmarks."""
    benchmark_manager = Benchmarker()

    load_profile = LoadProfile(load_profile)
    benchmark_manager.start_run(providers=providers, benchmark_names=benchmarks, load_profile=load_profile,
                                runtimes_to_include=runtimes, repetitions=repetitions)


# python benchmarker -p gcp -b echo/... --load-profile cold/warm/burst --repetitions 50
if __name__ == "__main__":
    main()

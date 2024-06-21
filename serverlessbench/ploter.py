import os
import json
import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
from matplotlib.ticker import LogFormatter

def read_json_files(base_path):
    data = []
    for root, dirs, files in os.walk(base_path):
        for file in files:
            if file.endswith(".json"):
                memory = file.split('_')[-1].replace('.json', '') 
                path_parts = root.split(os.sep)
                provider = path_parts[-3]
                execution_type = path_parts[-2]  
                function_name = path_parts[-1]
                try:
                    with open(os.path.join(root, file), 'r') as f:
                        json_data = json.load(f)
                        for key in json_data:
                            entry = {
                                "Function": function_name,
                                "Provider": provider,
                                "Memory": memory,
                                "ExecutionType": execution_type,
                                "client_time": json_data[key].get("client_time"),
                                "provider_time": json_data[key].get("provider_time"),
                                "results_time": json_data[key]["response_body"].get("results_time")
                            }
                            data.append(entry)
                except (KeyError, json.JSONDecodeError) as e:
                    print(f"Error {e} in file {file} for key {key}")
    return data

def create_boxplots(data, output_dir):
    df = pd.DataFrame(data)
    print("DataFrame columns:", df.columns)
    print("DataFrame head:", df.head())

    if 'Function' not in df.columns:
        print("Error: 'Function' column not found in DataFrame")
        return
    
    df_melted = pd.melt(df, id_vars=["Function", "Provider", "Memory", "ExecutionType"],
                        value_vars=["client_time", "provider_time", "results_time"],
                        var_name="TimeType", value_name="Time")

    for func in df['Function'].unique():
        for execution_type in ['jvm', 'native']:
            df_exec = df_melted[(df_melted['Function'] == func) & (df_melted['ExecutionType'] == execution_type)]
            if df_exec.empty:
                continue
            
            plt.figure(figsize=(16, 10))
            
            flierprops = dict(marker='D', markerfacecolor='grey', markeredgecolor='grey', markersize=5, linestyle='none')

            ax = sns.boxplot(x='Memory', y='Time', hue='TimeType', data=df_exec,
                             palette='pastel', flierprops=flierprops)

            plt.yscale('log')  
            ax.yaxis.set_major_formatter(LogFormatter(base=10.0))  

            providers = df_exec['Provider'].unique()
            memories = sorted(df_exec['Memory'].unique())
            ax.set_xticks(range(len(memories) * len(providers)))
            ax.set_xticklabels([f"{mem} ({prov})" for prov in providers for mem in memories])

            for idx in range(0, len(memories) * len(providers), len(memories)):
                plt.axvline(idx - 0.5, color='grey', linestyle='--')

            plt.title(f'{func} - {execution_type.upper()} Execution Times by Memory and Provider')
            plt.xlabel('Memory (MB) - Provider')
            plt.ylabel('Time (s)')
            plt.legend(title='Time Type', loc='upper right')
            plt.xticks(rotation=45)
            plt.grid(True)
            
            plot_file = os.path.join(output_dir, f'{func}_{execution_type}_boxplots.png')
            plt.savefig(plot_file, bbox_inches='tight')
            plt.close()
            print(f"Plot saved to {plot_file}")

def main():
    base_path = 'benchmark_results'
    output_dir = 'benchmark_plots'
    os.makedirs(output_dir, exist_ok=True)

    data = read_json_files(base_path)
    if not data:
        print("No data found.")
        return
    create_boxplots(data, output_dir)

if __name__ == "__main__":
    main()
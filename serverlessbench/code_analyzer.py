import os
import json
import re

def find_guice_classes(base_dir):
    guice_classes = set()
    package_pattern = re.compile(r'package\s+([\w\.]+);')
    inject_pattern = re.compile(r'import (com\.google\.inject\.Inject|jakarta\.inject\.Inject);')
    implemented_by_pattern = re.compile(r'import com\.google\.inject\.ImplementedBy;')
    class_declaration_pattern = re.compile(r'(public|interface) (?:abstract\s)?class\s+(\w+)|public (?:abstract\s)?class\s+(\w+)|public interface\s+(\w+)', re.MULTILINE)

    for root, _, files in os.walk(base_dir):
        for file in files:
            if file.endswith('.java'):
                file_path = os.path.join(root, file)
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if inject_pattern.search(content) or implemented_by_pattern.search(content):
                        package_match = package_pattern.search(content)
                        package_name = package_match.group(1) if package_match else ""
                        class_names = class_declaration_pattern.findall(content)
                        for class_name_tuple in class_names:
                            class_name = class_name_tuple[1] if class_name_tuple[1] else class_name_tuple[2]
                            if not class_name:
                                class_name = class_name_tuple[3]
                            if class_name:
                                full_class_name = f"{package_name}.{class_name}" if package_name else class_name
                                guice_classes.add(full_class_name)
    return guice_classes

def update_reflect_config(guice_classes, config_file):
    if not os.path.exists(config_file):
        print(f"{config_file} does not exist.")
        return

    with open(config_file, 'r', encoding='utf-8') as f:
        configurations = json.load(f)

    for config in configurations:
        if config["name"] in guice_classes:
            config["allDeclaredConstructors"] = True
            config["allPublicConstructors"] = True
            config["allDeclaredMethods"] = True
            config["allPublicMethods"] = True
            config.pop("queryAllDeclaredMethods", None)
            config.pop("queryAllDeclaredConstructors", None)
            config.pop("queryAllPublicMethods", None)
            config.pop("queryAllPublicConstructors", None)

    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(configurations, f, indent=2)

def write_guice_classes_to_file(guice_classes, output_file):
    with open(output_file, 'w', encoding='utf-8') as f:
        for class_name in sorted(guice_classes):
            f.write(f"{class_name}\n")

source_base_dir = '../../jclouds'
output_file = 'guice-classes.txt'
reflect_config_file = 'reflect-config.json'

guice_classes = find_guice_classes(source_base_dir)
write_guice_classes_to_file(guice_classes, output_file)
update_reflect_config(guice_classes, reflect_config_file)
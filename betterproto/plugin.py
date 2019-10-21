#!/usr/bin/env python

import itertools
import json
import os.path
import re
import sys
import textwrap
from typing import Any, List, Tuple

import jinja2

from google.protobuf.compiler import plugin_pb2 as plugin
from google.protobuf.descriptor_pb2 import (
    DescriptorProto,
    EnumDescriptorProto,
    FieldDescriptorProto,
    FileDescriptorProto,
    ServiceDescriptorProto,
)


def snake_case(value: str) -> str:
    return (
        re.sub(r"(?<=[a-z])[A-Z]|[A-Z](?=[^A-Z])", r"_\g<0>", value).lower().strip("_")
    )


def get_ref_type(package: str, imports: set, type_name: str) -> str:
    """
    Return a Python type name for a proto type reference. Adds the import if
    necessary.
    """
    type_name = type_name.lstrip(".")
    if type_name.startswith(package):
        # This is the current package, which has nested types flattened.
        type_name = f'"{type_name.lstrip(package).lstrip(".").replace(".", "")}"'

    if "." in type_name:
        # This is imported from another package. No need
        # to use a forward ref and we need to add the import.
        parts = type_name.split(".")
        imports.add(f"from .{'.'.join(parts[:-2])} import {parts[-2]}")
        type_name = f"{parts[-2]}.{parts[-1]}"

    return type_name


def py_type(
    package: str,
    imports: set,
    message: DescriptorProto,
    descriptor: FieldDescriptorProto,
) -> str:
    if descriptor.type in [1, 2, 6, 7, 15, 16]:
        return "float"
    elif descriptor.type in [3, 4, 5, 13, 17, 18]:
        return "int"
    elif descriptor.type == 8:
        return "bool"
    elif descriptor.type == 9:
        return "str"
    elif descriptor.type in [11, 14]:
        # Type referencing another defined Message or a named enum
        return get_ref_type(package, imports, descriptor.type_name)
    elif descriptor.type == 12:
        return "bytes"
    else:
        raise NotImplementedError(f"Unknown type {descriptor.type}")


def get_py_zero(type_num: int) -> str:
    zero = 0
    if type_num in []:
        zero = 0.0
    elif type_num == 8:
        zero = "False"
    elif type_num == 9:
        zero = '""'
    elif type_num == 11:
        zero = "None"
    elif type_num == 12:
        zero = 'b""'

    return zero


def traverse(proto_file):
    def _traverse(path, items):
        for i, item in enumerate(items):
            yield item, path + [i]

            if isinstance(item, DescriptorProto):
                for enum in item.enum_type:
                    enum.name = item.name + enum.name
                    yield enum, path + [i, 4]

                if item.nested_type:
                    for n, p in _traverse(path + [i, 3], item.nested_type):
                        # Adjust the name since we flatten the heirarchy.
                        n.name = item.name + n.name
                        yield n, p

    return itertools.chain(
        _traverse([5], proto_file.enum_type), _traverse([4], proto_file.message_type)
    )


def get_comment(proto_file, path: List[int]) -> str:
    for sci in proto_file.source_code_info.location:
        # print(list(sci.path), path, file=sys.stderr)
        if list(sci.path) == path and sci.leading_comments:
            lines = textwrap.wrap(
                sci.leading_comments.strip().replace("\n", ""), width=75
            )

            if path[-2] == 2 and path[-4] != 6:
                # This is a field
                return "    # " + "    # ".join(lines)
            else:
                # This is a message, enum, service, or method
                if len(lines) == 1 and len(lines[0]) < 70:
                    lines[0] = lines[0].strip('"')
                    return f'    """{lines[0]}"""'
                else:
                    joined = "\n    ".join(lines)
                    return f'    """\n    {joined}\n    """'

    return ""


def generate_code(request, response):
    env = jinja2.Environment(
        trim_blocks=True,
        lstrip_blocks=True,
        loader=jinja2.FileSystemLoader("%s/templates/" % os.path.dirname(__file__)),
    )
    template = env.get_template("template.py")

    output_map = {}
    for proto_file in request.proto_file:
        out = proto_file.package
        if not out:
            out = os.path.splitext(proto_file.name)[0].replace(os.path.sep, ".")

        if out not in output_map:
            output_map[out] = {"package": proto_file.package, "files": []}
        output_map[out]["files"].append(proto_file)

    # TODO: Figure out how to handle gRPC request/response messages and add
    # processing below for Service.

    for filename, options in output_map.items():
        package = options["package"]
        # print(package, filename, file=sys.stderr)
        output = {
            "package": package,
            "files": [f.name for f in options["files"]],
            "imports": set(),
            "typing_imports": set(),
            "messages": [],
            "enums": [],
            "services": [],
        }

        type_mapping = {}

        for proto_file in options["files"]:
            # print(proto_file.message_type, file=sys.stderr)
            # print(proto_file.service, file=sys.stderr)
            # print(proto_file.source_code_info, file=sys.stderr)

            for item, path in traverse(proto_file):
                # print(item, file=sys.stderr)
                # print(path, file=sys.stderr)
                data = {"name": item.name}

                if isinstance(item, DescriptorProto):
                    # print(item, file=sys.stderr)
                    if item.options.map_entry:
                        # Skip generated map entry messages since we just use dicts
                        continue

                    data.update(
                        {
                            "type": "Message",
                            "comment": get_comment(proto_file, path),
                            "properties": [],
                        }
                    )

                    for i, f in enumerate(item.field):
                        t = py_type(package, output["imports"], item, f)
                        zero = get_py_zero(f.type)

                        repeated = False
                        packed = False

                        field_type = f.Type.Name(f.type).lower()[5:]
                        map_types = None
                        if f.type == 11:
                            # This might be a map...
                            message_type = f.type_name.split(".").pop().lower()
                            # message_type = py_type(package)
                            map_entry = f"{f.name.replace('_', '').lower()}entry"

                            if message_type == map_entry:
                                for nested in item.nested_type:
                                    if (
                                        nested.name.replace("_", "").lower()
                                        == map_entry
                                    ):
                                        if nested.options.map_entry:
                                            # print("Found a map!", file=sys.stderr)
                                            k = py_type(
                                                package,
                                                output["imports"],
                                                item,
                                                nested.field[0],
                                            )
                                            v = py_type(
                                                package,
                                                output["imports"],
                                                item,
                                                nested.field[1],
                                            )
                                            t = f"Dict[{k}, {v}]"
                                            field_type = "map"
                                            map_types = (
                                                f.Type.Name(nested.field[0].type),
                                                f.Type.Name(nested.field[1].type),
                                            )
                                            output["typing_imports"].add("Dict")

                        if f.label == 3 and field_type != "map":
                            # Repeated field
                            repeated = True
                            t = f"List[{t}]"
                            zero = "[]"
                            output["typing_imports"].add("List")

                            if f.type in [1, 2, 3, 4, 5, 6, 7, 8, 13, 15, 16, 17, 18]:
                                packed = True

                        data["properties"].append(
                            {
                                "name": f.name,
                                "number": f.number,
                                "comment": get_comment(proto_file, path + [2, i]),
                                "proto_type": int(f.type),
                                "field_type": field_type,
                                "map_types": map_types,
                                "type": t,
                                "zero": zero,
                                "repeated": repeated,
                                "packed": packed,
                            }
                        )
                        # print(f, file=sys.stderr)

                    output["messages"].append(data)
                elif isinstance(item, EnumDescriptorProto):
                    # print(item.name, path, file=sys.stderr)
                    data.update(
                        {
                            "type": "Enum",
                            "comment": get_comment(proto_file, path),
                            "entries": [
                                {
                                    "name": v.name,
                                    "value": v.number,
                                    "comment": get_comment(proto_file, path + [2, i]),
                                }
                                for i, v in enumerate(item.value)
                            ],
                        }
                    )

                    output["enums"].append(data)

            for i, service in enumerate(proto_file.service):
                # print(service, file=sys.stderr)

                data = {
                    "name": service.name,
                    "comment": get_comment(proto_file, [6, i]),
                    "methods": [],
                }

                for j, method in enumerate(service.method):
                    if method.client_streaming:
                        raise NotImplementedError("Client streaming not yet supported")

                    input_message = None
                    input_type = get_ref_type(
                        package, output["imports"], method.input_type
                    ).strip('"')
                    for msg in output["messages"]:
                        if msg["name"] == input_type:
                            input_message = msg
                            for field in msg["properties"]:
                                if field["zero"] == "None":
                                    output["typing_imports"].add("Optional")
                            break

                    data["methods"].append(
                        {
                            "name": method.name,
                            "py_name": snake_case(method.name),
                            "comment": get_comment(proto_file, [6, i, 2, j]),
                            "route": f"/{package}.{service.name}/{method.name}",
                            "input": get_ref_type(
                                package, output["imports"], method.input_type
                            ).strip('"'),
                            "input_message": input_message,
                            "output": get_ref_type(
                                package, output["imports"], method.output_type
                            ).strip('"'),
                            "client_streaming": method.client_streaming,
                            "server_streaming": method.server_streaming,
                        }
                    )

                    if method.server_streaming:
                        output["typing_imports"].add("AsyncGenerator")

                output["services"].append(data)

        output["imports"] = sorted(output["imports"])
        output["typing_imports"] = sorted(output["typing_imports"])

        # Fill response
        f = response.file.add()
        # print(filename, file=sys.stderr)
        f.name = filename.replace(".", os.path.sep) + ".py"

        # f.content = json.dumps(output, indent=2)
        f.content = template.render(description=output).rstrip("\n") + "\n"

    inits = set([""])
    for f in response.file:
        # Ensure output paths exist
        # print(f.name, file=sys.stderr)
        dirnames = os.path.dirname(f.name)
        if dirnames:
            os.makedirs(dirnames, exist_ok=True)
            base = ""
            for part in dirnames.split(os.path.sep):
                base = os.path.join(base, part)
                inits.add(base)

    for base in inits:
        init = response.file.add()
        init.name = os.path.join(base, "__init__.py")
        init.content = b""


def main():
    """The plugin's main entry point."""
    # Read request message from stdin
    data = sys.stdin.buffer.read()

    # Parse request
    request = plugin.CodeGeneratorRequest()
    request.ParseFromString(data)

    # Create response
    response = plugin.CodeGeneratorResponse()

    # Generate code
    generate_code(request, response)

    # Serialise response message
    output = response.SerializeToString()

    # Write to stdout
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    main()
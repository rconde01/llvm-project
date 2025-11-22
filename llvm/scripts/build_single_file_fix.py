# This script is a janky workaround for building a single file within Visual Studio given our
# current configuration.
# See:
#   * https://developercommunity.visualstudio.com/t/Unknown-Target-Error-when-building-singl/10294383
#   * https://developercommunity.visualstudio.com/t/Unknown-Target-Error-when-building-singl/10436412
#   * https://developercommunity.visualstudio.com/t/Unknown-Target-Error-when-building-singl/10566593
#   * https://developercommunity.visualstudio.com/t/Build-single-file-failed-with-ninja:-er/10636057
#
# The first 3 issues were fixed, but then they decided the last issue is not a bug, but a suggestion.
# (╯°□°)╯︵ ┻━┻
#
# This script provides a solution. You need to set up custom tools in Visual Studio to make it work:
#    Title:             file build
#    Command:           python.exe
#    Arguments:         $(SolutionDir)build-single-file-fix.py "$(ItemPath)"
#    Initial directory: $(SolutionDir)
#    Use Output Window: checked
#
# You can assign a shortcut to the tool item by:
#   * Determining the command index (they are numbers 1-N based on the order in the tool menu).
#   * Go to Tools->Options->Environment->Keyboard
#   * In 'Show commands containing' type: Tools.ExternalCommandN, where N is the command you're looking for
#   * Enter the shortcut and assign.
#
# This script requires `vswhere`. Install it via `choco install vswhere` (or your favorite package manager).
#
# The script works by:
#    * Taking the passed file
#    * Finding the path relative to cpp
#    * Loading the build.ninja file from the known location
#    * Finding the matching target name
#    * Executing the ninja build for that target

import sys
import os
import subprocess
import json

script_dir = os.path.dirname(os.path.realpath(__file__))

# This is a known location in our config
cmake_bin_root_dir = os.path.abspath(os.path.join(script_dir, "..", "build"))

toolset = "14.42"
vs_version = "17"


# Split the path into file and folders (removing the drive)
def get_path_components(path: str):
    path = os.path.normpath(path)
    drive, path_and_file = os.path.splitdrive(path)

    path, file = os.path.split(path_and_file)

    components = path.split(os.path.sep)

    components.append(file)

    # our root is ff so end there
    components_to_use = []

    for c in reversed(components):
        components_to_use.insert(0, c)

        if c == "ff":
            break

    return components_to_use


# Get the build config selected in the IDE
def get_current_build_config_folder():
    # We can hack out the current build config from this internal file
    vs_project_settings_path = os.path.join(
        script_dir, "..", ".vs", "ProjectSettings.json"
    )

    if not os.path.exists(vs_project_settings_path):
        raise Exception(
            f'Could not find VS project settings at "{vs_project_settings_path}".'
        )

    with open(vs_project_settings_path) as f:
        config = (
            "debug"
            if "debug" in json.load(f)["CurrentProjectSetting"].lower()
            else "release"
        )

    return config


def get_vs_tools_bat():
    # For ninja to build properly we need to apply the VsDevCmd.bat file
    # Note: [{vs_version}.0,{vs_version}.100] just ensures we just get the major version we want (i.e. vs2022)
    vs_install_dir = (
        subprocess.check_output(f"vswhere -property installationPath -version [{vs_version}.0,{vs_version}.100]")
        .decode()
        .strip("\n\r")
    )

    vs_dev_cmd_path = os.path.join(vs_install_dir, "Common7", "tools", "VsDevCmd.bat")

    if not os.path.exists(vs_dev_cmd_path):
        raise Exception(
            f'Could not find VsDevCmd.bat at the expected location "{vs_dev_cmd_path}".'
        )

    return vs_dev_cmd_path

def build_ninja_target(vs_dev_cmd_path, cmake_bin_dir, target_name):
    # -nologo simply suppresses extra output from the batch file
    subprocess.check_call(
        f'{vs_dev_cmd_path} -no_logo -arch=amd64 -vcvars_ver={toolset} && ninja -C"{cmake_bin_dir}" "{target_name}^"'
    )
    print("Done.", flush=True)
    print("-" * 80, flush=True)


if __name__ == "__main__":
    usage = "usage: <file_to_compile>"

    if len(sys.argv) != 2:
        raise usage

    file_to_compile = sys.argv[1]

    if not os.path.exists(file_to_compile):
        raise Exception(f'File "{file_to_compile}" does not exist.')

    config_dir = get_current_build_config_folder()
    cmake_bin_dir = os.path.join(cmake_bin_root_dir, config_dir)
    vs_dev_cmd_path = get_vs_tools_bat()

    print(f"file:          {file_to_compile}", flush=True)
    print(f"configuration: {config_dir}", flush=True)
    print(f"cmake bin dir: {cmake_bin_dir}", flush=True)
    print(f"VS Dev Cmd:    {vs_dev_cmd_path}", flush=True)
    print(f"Toolset:       {toolset}", flush=True)
    print(f"VS Version:    {vs_version}", flush=True)
    print("-" * 80, flush=True)

    build_ninja_target(vs_dev_cmd_path, cmake_bin_dir, file_to_compile)
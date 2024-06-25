from typing import Union, cast
from dataclasses import dataclass
import hashlib
import re
import platform

from datasets import Dataset, load_dataset
from swebench.harness.swebench_instance import SwebenchInstance
from swebench.harness.dockerfiles import (
    get_dockerfile_base,
    get_dockerfile_env,
    get_dockerfile_instance,
)
from swebench.harness.utils import (
    get_requirements,
    get_environment_yml,
    get_test_directives,
)
from swebench.harness.constants import (
    MAP_REPO_TO_INSTALL,
    MAP_VERSION_TO_INSTALL,
    MAP_REPO_TO_TEST_FRAMEWORK,
    USE_X86,
)

DIFF_MODIFIED_FILE_REGEX = r"--- a/(.*)"


def load(name="princeton-nlp/SWE-bench", split="test") -> list[SwebenchInstance]:
    dataset = cast(Dataset, load_dataset(name, split=split))
    return [cast(SwebenchInstance, instance) for instance in dataset]


@dataclass
class TestSpec:
    instance_id: str
    repo: str
    version: str
    repo_script_list: str
    eval_script_list: str
    env_script_list: str
    arch: str

    @property
    def setup_env_script(self):
        return "\n".join(["#!/bin/bash", "set -euxo pipefail"] + self.env_script_list) + "\n"

    @property
    def eval_script(self):
        return "\n".join(["#!/bin/bash", "set -uxo pipefail"] + self.eval_script_list) + "\n"
        # Don't exit early because we need to revert tests at the end

    @property
    def install_repo_script(self):
        return "\n".join(["#!/bin/bash", "set -euxo pipefail"] + self.repo_script_list) + "\n"

    @property
    def base_image_key(self):
        return f"sweb.base.{self.arch}:latest"

    @property
    def env_image_key(self):
        hash_object = hashlib.sha256()
        hash_object.update(str(self.env_script_list).encode("utf-8"))
        hash_value = hash_object.hexdigest()
        val = hash_value[:22]  # 22 characters is still very likely to be unique
        return f"sweb.env.{self.arch}.{val}:latest"

    @property
    def instance_image_key(self):
        return f"sweb.eval.{self.arch}.{self.instance_id}:latest"

    def get_instance_container_name(self, session_id=None):
        if not session_id:
            return f"sweb.eval.{self.instance_id}"
        return f"sweb.eval.{self.instance_id}.{session_id}"

    @property
    def base_dockerfile(self):
        return get_dockerfile_base(self.platform, self.arch)

    @property
    def env_dockerfile(self):
        return get_dockerfile_env(self.platform, self.arch)

    @property
    def instance_dockerfile(self):
        return get_dockerfile_instance(self.platform, self.env_image_key)

    @property
    def platform(self):
        if self.arch == "x86_64":
            return "linux/x86_64"
        elif self.arch == "arm64":
            return "linux/arm64/v8"
        else:
            raise ValueError(f"Invalid architecture: {self.arch}")
        

def get_test_specs_from_dataset(dataset: Union[list[SwebenchInstance], list[TestSpec]]) -> list[TestSpec]:
    if isinstance(dataset[0], TestSpec):
        return dataset
    return list(map(make_test_spec, dataset))


def make_repo_script_list(install, repo, repo_directory, base_commit, env_name):
    setup_commands = [
        f"git clone -o origin https://github.com/{repo} {repo_directory}",
        f"chmod -R 777 {repo_directory}",  # So nonroot user can run tests
        f"cd {repo_directory}",
        f"git reset --hard {base_commit}",
        # Remove the remote so the agent won't see newer commits.
        f"git remote remove origin",
        # Make sure conda is available for later use
        "source /opt/miniconda3/bin/activate",
        f"conda activate {env_name}",
        f'echo "Current environment: $CONDA_DEFAULT_ENV"',
    ]
    if repo in MAP_REPO_TO_INSTALL:
        setup_commands.append(MAP_REPO_TO_INSTALL[repo])

    # Run pre-install set up if provided
    if "pre_install" in install:
        for pre_install in install["pre_install"]:
            setup_commands.append(pre_install)

    if "install" in install:
        setup_commands.append(install["install"])
    return setup_commands


def make_env_script_list(instance, install, env_name):
    HEREDOC_DELIMITER = "EOF_59812759871"
    reqs_commands = [
        "source /opt/miniconda3/bin/activate",
    ]
    # Create conda environment according to install instructinos
    pkgs = install.get("packages", "")
    if pkgs == "requirements.txt":
        # Create environment
        cmd = f"conda create -n {env_name} python={install['python']} -y"
        reqs_commands.append(cmd)

        # Install dependencies
        reqs = get_requirements(instance)
        path_to_reqs = "$HOME/requirements.txt"
        reqs_commands.append(
            f"cat <<'{HEREDOC_DELIMITER}' > {path_to_reqs}\n{reqs}\n{HEREDOC_DELIMITER}"
        )
        cmd = f"conda activate {env_name} && python -m pip install -r {path_to_reqs}"
        reqs_commands.append(cmd)
        reqs_commands.append(f"rm {path_to_reqs}")
    elif pkgs == "environment.yml":
        # Create environment from yml
        reqs = get_environment_yml(instance, env_name)
        path_to_reqs = "environment.yml"
        reqs_commands.append(
            f"cat <<'{HEREDOC_DELIMITER}' > {path_to_reqs}\n{reqs}\n{HEREDOC_DELIMITER}"
        )
        if "no_use_env" in install and install["no_use_env"]:
            # `conda create` based installation
            cmd = f"conda create -c conda-forge -n {env_name} python={install['python']} -y"
            reqs_commands.append(cmd)

            # Install dependencies
            cmd = f"conda env update -f {path_to_reqs}"
            reqs_commands.append(cmd)
        else:
            # `conda env create` based installation
            cmd = f"conda env create --file {path_to_reqs}"
            reqs_commands.append(cmd)

            cmd = f"conda activate {env_name} && conda install python={install['python']} -y"
            reqs_commands.append(cmd)

        # Remove environment.yml
        reqs_commands.append(f"rm {path_to_reqs}")
    else:
        # Create environment + install dependencies
        cmd = f"conda create -n {env_name} python={install['python']} {pkgs} -y"
        reqs_commands.append(cmd)

    reqs_commands.append(f"conda activate {env_name}")

    # Install additional packages if specified
    if "pip_packages" in install:
        pip_packages = " ".join(install["pip_packages"])
        cmd = f"python -m pip install {pip_packages}"
        reqs_commands.append(cmd)
    return reqs_commands


def make_eval_script_list(instance, install, env_name, repo_directory, base_commit, test_patch):
    HEREDOC_DELIMITER = "EOF_114329324912"
    # Reset test files to the state they should be in before the patch.
    test_files = re.findall(DIFF_MODIFIED_FILE_REGEX, test_patch)
    reset_tests_command = f"git checkout {base_commit} {' '.join(test_files)}"
    apply_test_patch_command = (
        f"git apply -v - <<'{HEREDOC_DELIMITER}'\n{test_patch}\n{HEREDOC_DELIMITER}"
    )
    test_command = " ".join(
        [
            MAP_REPO_TO_TEST_FRAMEWORK[instance["repo"]][instance["version"]],
            *get_test_directives(instance),
        ]
    )
    eval_commands = [
        f"source /opt/miniconda3/bin/activate",
        f"conda activate {env_name}",
        f"cd {repo_directory}",
    ]
    if "eval_commands" in install:
        eval_commands += install["eval_commands"]
    eval_commands += [
        f"git config --global --add safe.directory {repo_directory}",  # for nonroot user
        f"cd {repo_directory}",
        # This is just informational, so we have a record
        f"git status",
        f"git show",
        f"git diff {base_commit}",
        "source /opt/miniconda3/bin/activate",
        f"conda activate {env_name}",
    ]
    if "install" in install:
        eval_commands.append(install["install"])
    eval_commands += [
        reset_tests_command,
        apply_test_patch_command,
        test_command,
        reset_tests_command,  # Revert tests after done, leave the repo in the same state as before
    ]
    return eval_commands


def make_test_spec(instance: SwebenchInstance) -> TestSpec:
    if isinstance(instance, TestSpec):
        return instance
    instance_id = instance["instance_id"]
    repo = instance["repo"]
    version = instance["version"]
    base_commit = instance["base_commit"]
    problem_statement = instance["problem_statement"]
    hints_text = instance["hints_text"]  # Unused
    test_patch = instance["test_patch"]

    env_name = "testbed"
    repo_directory = f"/{env_name}"
    install = MAP_VERSION_TO_INSTALL[repo][version]

    repo_script_list = make_repo_script_list(install, repo, repo_directory, base_commit, env_name)
    env_script_list = make_env_script_list(instance, install, env_name)
    eval_script_list = make_eval_script_list(
        instance, install, env_name, repo_directory, base_commit, test_patch
    )
    if platform.machine() in {"aarch64", "arm64"}:
        arch = "arm64" if instance_id not in USE_X86 else "x86_64"
    else:
        arch = "x86_64"

    return TestSpec(
        instance_id=instance_id,
        repo=repo,
        env_script_list=env_script_list,
        repo_script_list=repo_script_list,
        eval_script_list=eval_script_list,
        version=version,
        arch=arch,
    )

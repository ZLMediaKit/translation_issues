# -*- coding:utf-8 -*-
__author__ = 'alex'

import os
import subprocess
import sys

from core import settings
from core.console import console
from core.log import logger


def check_installed(command):
    """
    Check if a command exists
    :param command:
    :return:
    """
    if os.system(f"command -v {command} > /dev/null") == 0:
        return True
    else:
        return False


def run_cmd(cmd, assert_success=False, capture_output=False, env=None):
    """
    Run a command and return the result or error, without displaying the run output
    :param cmd:
    :param assert_success:
    :param capture_output:
    :param env:
    :return:
    """
    if not env:
        env = os.environ.copy()
    result = subprocess.run(cmd, shell=True, env=env, capture_output=capture_output)
    # Assert the command ran successfully
    if assert_success and result.returncode != 0:
        console.print("Command '" + cmd + "' failed with exit status code '" + str(
            result.returncode) + "'.\n\nExiting now.\nTry running the script again.",
                      new_line_start=True, style="bold red")
        console.print(result.stderr.decode(), style="bold red")
        sys.stdout.flush()
        sys.stderr.flush()
        sys.exit(1)
    return True


def check_env():
    """
    check environment
    :return:
    """
    # check python version
    console.log("Please wait for the environment to be checked\n")
    console.status("Checking environment")
    if sys.version_info < (3, 11):
        console.log("Python version must be 3.11 or above\n", style="bold red")
        sys.exit(1)
    if not (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix):
        console.log("Please run this script in virtualenv\n", style="bold red")
        sys.exit(1)
    console.log("Python version is ok\n", style="bold green")


def create_pid_file():
    pid = str(os.getpid())
    if not os.path.exists(os.path.dirname(settings.PID_FILE)):
        os.makedirs(os.path.dirname(settings.PID_FILE))
    with open(settings.PID_FILE, "w") as f:
        f.write(pid)


def delete_pid_file():
    if os.path.exists(settings.PID_FILE):
        os.remove(settings.PID_FILE)


def check_pid_status():
    if os.path.exists(settings.PID_FILE):
        with open(settings.PID_FILE, "r") as f:
            pid = f.read()
        if os.path.exists("/proc/%s" % pid):
            logger.error("Server already started")
            return False
        else:
            logger.info("Server starting")
            create_pid_file()
    else:
        logger.info("Server starting")
        create_pid_file()
    return True

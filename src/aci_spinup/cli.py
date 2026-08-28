from __future__ import annotations

import argparse
import sys

from . import __version__
from .deploy import (
    add_deploy_arguments,
    request_from_args,
    run_deploy,
)
from .errors import AciSpinupError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aci-spinup",
        description="Deploy compliant Azure Container Instances.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    deploy_parser = commands.add_parser(
        "deploy",
        help="Deploy ACI nodes, or safely delete their resource group",
    )
    add_deploy_arguments(deploy_parser)
    deploy_parser.set_defaults(command_parser=deploy_parser)

    return parser


def _report_error(error: AciSpinupError) -> int:
    print(f"error: {error}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "deploy":
            request = request_from_args(args.command_parser, args)
            return run_deploy(request)
    except AciSpinupError as error:
        return _report_error(error)
    parser.error(f"unsupported command: {args.command}")
    return 2

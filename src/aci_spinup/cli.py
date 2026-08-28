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
from .repair import (
    add_repair_arguments,
    config_from_args,
    run_repair,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aci-spinup",
        description=(
            "Deploy Azure Container Instances or repair explicit subnet "
            "outbound connectivity."
        ),
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

    repair_parser = commands.add_parser(
        "repair-subnet-outbound",
        help="Attach one NAT gateway to unassociated VNet subnets",
    )
    add_repair_arguments(repair_parser)
    repair_parser.set_defaults(command_parser=repair_parser)
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
        if args.command == "repair-subnet-outbound":
            config = config_from_args(args.command_parser, args)
            return run_repair(
                config,
                dry_run=args.dry_run,
                output=args.output,
                subscription=args.subscription,
                verbose=args.verbose,
            )
    except AciSpinupError as error:
        return _report_error(error)
    parser.error(f"unsupported command: {args.command}")
    return 2

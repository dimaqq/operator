# Copyright 2024 Canonical Ltd.

"""Updates pinned versions of charms in tests."""

import logging
import os
import sys

from httpx import Client
from ruamel.yaml import YAML

yaml = YAML(typ="rt")
yaml.indent(mapping=2, sequence=4, offset=2)

github = Client(
    base_url="https://api.github.com/repos",
    headers={
        "Authorization": f'token {os.getenv("GITHUB_TOKEN")}',
        "Accept": "application/vnd.github.v3+json",
    },
)


def update_charm_pins(workflow):
    """Update pinned versions of charms in the given github actions workflow."""
    with open(workflow) as file:
        doc = yaml.load(file)

    # Assume that there's only one job, or that the first job is parametrised with charm repos
    job_name = next(iter(doc["jobs"]))

    for idx, item in enumerate(doc["jobs"][job_name]["strategy"]["matrix"]["include"]):
        charm_repo = item["charm-repo"]

        resp = github.get(f"{charm_repo}/commits", params={"per_page": 1})
        resp.raise_for_status()
        commit = resp.json()[0]["sha"]
        timestamp = resp.json()[0]["commit"]["committer"]["date"]

        resp = github.get(f"{charm_repo}/tags")
        resp.raise_for_status()
        comment = " ".join(
            [tag["name"] for tag in resp.json() if tag["commit"]["sha"] == commit]
            + [timestamp]
        )

        node = doc.mlget(
            ["jobs", job_name, "strategy", "matrix", "include", idx], list_ok=True
        )
        node["commit"] = commit
        node.yaml_add_eol_comment(comment, key="commit")

    with open(workflow, "w") as file:
        yaml.dump(doc, file)


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    for workflow in sys.argv[1].split():
        update_charm_pins(workflow)

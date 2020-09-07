#!/usr/bin/env python3
import os
import asyncio
import subprocess
from typing import List, Optional
import re
from pathlib import Path
import sys

import aiohttp
from gidgethub.aiohttp import GitHubAPI
from semantic_release.history import angular_parser, get_new_version
from semantic_release.errors import UnknownCommitMessageStyleError
from semantic_release.history.logs import LEVELS
from semantic_release.history.parser_helpers import ParsedCommit
import sh

git = sh.git

def run(cmd):
    return subprocess.check_output(cmd).decode("utf-8").strip()

def get_repo():
    # origin = run(["git", "remote", "get-url", "origin"])
    repo = os.environ.get("GITHUB_REPOSITORY", None)
    if repo is not None:
        return repo

    origin = git.remote("get-url", "origin")
    _, loc = origin.split(":", 1)
    repo, _ = loc.split(".", 1)
    return repo

def get_current_version():
    raw = git.describe().split("-")[0]
    m = re.match(r"v(\d+\.\d+\.\d+)", raw)
    return m.group(1)

class Commit():
  sha: str
  message: str

  def __init__(self, sha:str, message: str):
    self.sha = sha
    self.message = self._normalize(message)
  
  @staticmethod
  def _normalize(message):
    message = message.replace("\r", "\n")
    return message

  def __str__(self):
    message = self.message.split("\n")[0]
    return f"Commit(sha='{self.sha[:8]}', message='{message}')"

_default_parser = angular_parser

def evaluate_version_bump(commits: List[Commit], commit_parser=_default_parser) -> Optional[str]:
    """
    Adapted from: https://github.com/relekang/python-semantic-release/blob/master/semantic_release/history/logs.py#L22
    """
    bump = None

    changes = []
    commit_count = 0

    for commit in commits:
        commit_count += 1
        try:
            message = commit_parser(commit.message)
            changes.append(message.bump)
        except UnknownCommitMessageStyleError as err:
            pass

    if changes:
        level = max(changes)
        if level in LEVELS:
            bump = LEVELS[level]
        else:
            print(f"Unknown bump level {level}")

    return bump

def generate_changelog(commits, commit_parser=_default_parser) -> dict:
    """
    Modified from: https://github.com/relekang/python-semantic-release/blob/48972fb761ed9b0fb376fa3ad7028d65ff407ee6/semantic_release/history/logs.py#L78
    """
    changes: dict = {"breaking": []}

    for commit in commits:
        try:
            message: ParsedCommit = commit_parser(commit.message)
            if message.type not in changes:
                changes[message.type] = list()

            capital_message = (
                message.descriptions[0][0].upper() + message.descriptions[0][1:]
            )
            changes[message.type].append((commit.sha, capital_message))

            if message.breaking_descriptions:
                for paragraph in message.breaking_descriptions:
                    changes["breaking"].append((commit.sha, paragraph))
            elif message.bump == 3:
                changes["breaking"].append((commit.sha, message.descriptions[0]))

        except UnknownCommitMessageStyleError as err:
            pass

    return changes


def markdown_changelog(
    version: str, changelog: dict, header: bool = False,
) -> str:
    output = f"## v{version}\n" if header else ""

    for section, items in changelog.items():
        if len(items) == 0:
            continue
        output += "\n### {0}\n".format(section.capitalize())

        for item in items:
            output += "* {0} ({1})\n".format(item[1], item[0])

    return output


async def main():
    token = os.environ["GH_TOKEN"]
    async with aiohttp.ClientSession(loop=asyncio.get_event_loop()) as session:
      gh = GitHubAPI(session, __name__, oauth_token=token)

      version_file = Path("version_number")
      current_version = version_file.read_text()

      tag_hash = str(git("rev-list", "-n", "1", f"v{current_version}").strip())
      print("current_version:", current_version, "["+tag_hash[:8]+"]")

      sha = git("rev-parse", "HEAD").strip()
      print("sha:", sha)

      repo = get_repo()
      print("repo:", repo)

      commits_iter = gh.getiter(f"/repos/{repo}/commits?sha={sha}")

      commits = []

      async for item in commits_iter:
          commit_hash = item["sha"]
          commit_message = item["commit"]["message"]
          if commit_hash == tag_hash:
              break
          commit = Commit(commit_hash, commit_message)
          commits.append(commit)
          print("-", commit)

      if len(commits) > 100:
          print(len(commits), "are a lot. Aborting!")
          sys.exit(1)

      bump = evaluate_version_bump(commits)
      print("bump:", bump)
      if bump is None:
          print("-> nothing to do")
          return
      next_version = get_new_version(current_version, bump)
      print("next version:", next_version)
      next_tag = f"v{next_version}"

      changes = generate_changelog(commits)
      md = markdown_changelog(next_version, changes, header=True)

      print(md)

      version_file.write_text(next_version)

      git.add(version_file)
      git.commit(m=f"Bump to version {next_tag}")

      git.tag(next_tag)

      git.push("--follow-tags")
      for _ in range(5):
          _all_tags = await gh.getitem(f"/repos/{repo}/tags")
          all_tags = [t["name"] for t in _all_tags]
          if next_tag in all_tags:
              break
          await asyncio.sleep(0.5) # make sure the tag is visible for github

      await gh.post(f"/repos/{repo}/releases", data={"body": md, "tag_name": next_tag})


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())

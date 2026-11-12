# Contributing to Day Ahead Optimizer

Thank you for helping improve Day Ahead Optimizer. This page explains the
legal side of contributing. For the technical workflow (development
environment, tests, branches and pull requests) see [DEVELOP.md](DEVELOP.md).

## License of your contribution

Day Ahead Optimizer is licensed under the [Apache License 2.0](LICENSE.txt).
Contributions follow the "inbound = outbound" rule of section 5 of that
license: anything you submit for inclusion is licensed under the same
Apache License 2.0, with no additional terms.

- There is **no Contributor License Agreement (CLA)** and **no copyright
  assignment**. You keep the copyright of your contribution.
- Your contribution cannot be relicensed under other terms without your
  consent; everyone, including the maintainers, uses it under Apache 2.0.
- The license protects you as a contributor in the same way it protects the
  maintainers: your work is provided "as is", without warranty (section 7),
  and you are not liable for damages arising from its use (section 8).
- Every contributor grants, and receives from all other contributors, the
  patent license of section 3. That license ends for anyone who starts patent
  litigation claiming the project infringes a patent.
- Third-party code may only be added with its license and copyright notice
  intact. Record it in [NOTICE](NOTICE) and [REUSE.toml](REUSE.toml), and add
  its license text to `LICENSES/` if it is not there yet.

## Sign your commits off (DCO)

Every commit must carry a `Signed-off-by` line certifying the
[Developer Certificate of Origin 1.1](DCO). By signing off you state that you
wrote the change, or otherwise have the right to submit it under the
project's open source license. A pull request check verifies the sign-off on
each new commit; earlier history is not affected.

Use your real name or the name you are known by in the project, and an
address you control (the GitHub `noreply` address works):

```bash
git config user.name "Your Name"
git config user.email "12345+your-login@users.noreply.github.com"
git commit -s -m "Describe the change"
```

This adds:

```text
Signed-off-by: Your Name <12345+your-login@users.noreply.github.com>
```

Forgot it? Add it to the last commit with `git commit --amend -s --no-edit`,
or to every commit of your branch with
`git rebase --signoff origin/main`, then force-push your branch.

## Credit for shared work

When a commit includes work by someone else, add a trailer for each person:

```text
Co-authored-by: Their Name <their@address>
```

Co-authors are credited in [AUTHORS.md](AUTHORS.md) like commit authors.
Use this trailer only for people; automated tools and bots are not listed
as authors.

## Being listed in AUTHORS.md

[AUTHORS.md](AUTHORS.md) is generated from the git history by
`python scripts/update_authors.py`. If you appear under several names or
addresses, or want a different display name, add a line to
[.mailmap](.mailmap) in your pull request and run the script.

## Copyright notices in files

Copyright and license information for all files is kept centrally in
[REUSE.toml](REUSE.toml), following the [REUSE](https://reuse.software)
specification, so you do not need to add headers to new files. Check
compliance with:

```bash
pip install reuse
reuse lint
```

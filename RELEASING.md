# Releasing EarthForge

EarthForge is published on PyPI as **`pyearthforge`** by the GitHub Action in
[`.github/workflows/publish.yml`](.github/workflows/publish.yml). It uses PyPI
*trusted publishing*: GitHub proves to PyPI which workflow is uploading, so no
API tokens are needed or stored.

## One-time setup

Do this once on **PyPI** and once on **TestPyPI**, so each trusts this repository's workflow.
The project doesn't need to exist yet ("pending publisher").

1. Create accounts on <https://pypi.org> and <https://test.pypi.org> and turn on two-factor authentication.
2. On each site, go to **Your account → Publishing → Add a new pending publisher → GitHub** and enter:

   | Field | PyPI | TestPyPI |
   |---|---|---|
   | PyPI project name | `pyearthforge` | `pyearthforge` |
   | Owner | `msndma2025` | `msndma2025` |
   | Repository name | `EarthForge` | `EarthForge` |
   | Workflow name | `publish.yml` | `publish.yml` |
   | Environment name | `pypi` | `testpypi` |

3. Optional, recommended: in the GitHub repository, **Settings → Environments → pypi →
   Required reviewers**, add yourself. Each upload to PyPI then waits for your approval.

## Rehearse on TestPyPI

1. In the GitHub repository, open **Actions → Publish to PyPI → Run workflow** (branch `main`).
2. When it finishes, check <https://test.pypi.org/project/pyearthforge/> and try it:

   ```bash
   pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ pyearthforge
   ```

TestPyPI also refuses the same version twice; to rehearse again, raise the version first.

## Publish a release

1. Update the version in `pyearthforge/__init__.py`, e.g. `__version__ = "0.1.1"`.
   PyPI never accepts the same version twice.
2. In `CHANGELOG.md`, rename the `[Unreleased]` section to the new version and date.
3. Commit and push.
4. On GitHub: **Releases → Draft a new release → Choose a tag → type `v0.1.1` → Create new tag**,
   add a title and notes (you can paste the CHANGELOG section), and click **Publish release**.

The workflow then builds the package, checks that the tag (`v0.1.1`) matches `__version__`
(`0.1.1`), installs and imports it as a test, and uploads it to
<https://pypi.org/project/pyearthforge/>. Follow it under the **Actions** tab.

## Building by hand

```bash
python -m pip install --upgrade build twine
python -m build                  # creates dist/pyearthforge-<version>-py3-none-any.whl and .tar.gz
python -m twine check dist/*
python -m twine upload dist/*    # manual upload: username __token__, password a PyPI API token
```

Delete old files in `dist/` before uploading by hand.

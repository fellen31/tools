"""Downloads a nf-core pipeline to the local file system."""

from __future__ import print_function

import concurrent.futures
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import textwrap
from datetime import datetime
from zipfile import ZipFile

import git
import questionary
import requests
import requests_cache
import rich
import rich.progress
from git.exc import GitCommandError, InvalidGitRepositoryError
from pkg_resources import parse_version as VersionParser

import nf_core
import nf_core.list
import nf_core.utils
from nf_core.synced_repo import RemoteProgressbar, SyncedRepo
from nf_core.utils import (
    NFCORE_CACHE_DIR,
    NFCORE_DIR,
    SingularityCacheFilePathValidator,
)

log = logging.getLogger(__name__)
stderr = rich.console.Console(
    stderr=True, style="dim", highlight=False, force_terminal=nf_core.utils.rich_force_colors()
)


class DownloadProgress(rich.progress.Progress):
    """Custom Progress bar class, allowing us to have two progress
    bars with different columns / layouts.
    """

    def get_renderables(self):
        for task in self.tasks:
            if task.fields.get("progress_type") == "summary":
                self.columns = (
                    "[magenta]{task.description}",
                    rich.progress.BarColumn(bar_width=None),
                    "[progress.percentage]{task.percentage:>3.0f}%",
                    "•",
                    "[green]{task.completed}/{task.total} completed",
                )
            if task.fields.get("progress_type") == "download":
                self.columns = (
                    "[blue]{task.description}",
                    rich.progress.BarColumn(bar_width=None),
                    "[progress.percentage]{task.percentage:>3.1f}%",
                    "•",
                    rich.progress.DownloadColumn(),
                    "•",
                    rich.progress.TransferSpeedColumn(),
                )
            if task.fields.get("progress_type") == "singularity_pull":
                self.columns = (
                    "[magenta]{task.description}",
                    "[blue]{task.fields[current_log]}",
                    rich.progress.BarColumn(bar_width=None),
                )
            yield self.make_tasks_table([task])


class DownloadWorkflow:
    """Downloads a nf-core workflow from GitHub to the local file system.

    Can also download its Singularity container image if required.

    Args:
        pipeline (str): A nf-core pipeline name.
        revision (List[str]): The workflow revision to download, like `1.0`. Defaults to None.
        container (bool): Flag, if the Singularity container should be downloaded as well. Defaults to False.
        tower (bool): Flag, to customize the download for Nextflow Tower (convert to git bare repo). Defaults to False.
        outdir (str): Path to the local download directory. Defaults to None.
    """

    def __init__(
        self,
        pipeline=None,
        revision=None,
        outdir=None,
        compress_type=None,
        force=False,
        tower=False,
        container=None,
        singularity_cache=None,
        singularity_cache_index=None,
        parallel_downloads=4,
    ):
        self.pipeline = pipeline
        if isinstance(revision, str):
            self.revision = [revision]
        elif isinstance(revision, tuple):
            self.revision = [*revision]
        else:
            self.revision = []
        self.outdir = outdir
        self.output_filename = None
        self.compress_type = compress_type
        self.force = force
        self.tower = tower
        self.include_configs = None
        # force download of containers if a cache index is given or download is meant to be used for Tower.
        self.container = "singularity" if singularity_cache_index or bool(tower) else container
        # if a singularity_cache_index is given, use the file and overrule choice.
        self.singularity_cache = "remote" if singularity_cache_index else singularity_cache
        self.singularity_cache_index = singularity_cache_index
        self.parallel_downloads = parallel_downloads

        self.wf_revisions = {}
        self.wf_branches = {}
        self.wf_sha = {}
        self.wf_download_url = {}
        self.nf_config = {}
        self.containers = []
        self.containers_remote = []  # stores the remote images provided in the file.

        # Fetch remote workflows
        self.wfs = nf_core.list.Workflows()
        self.wfs.get_remote_workflows()

    def download_workflow(self):
        """Starts a nf-core workflow download."""

        # Get workflow details
        try:
            self.prompt_pipeline_name()
            self.pipeline, self.wf_revisions, self.wf_branches = nf_core.utils.get_repo_releases_branches(
                self.pipeline, self.wfs
            )
            self.prompt_revision()
            self.get_revision_hash()
            # Inclusion of configs is unnecessary for Tower.
            if not self.tower and self.include_configs is None:
                self.prompt_config_inclusion()
            # If a remote cache is specified, it is safe to assume images should be downloaded.
            if not self.singularity_cache == "remote":
                self.prompt_container_download()
            else:
                self.container = "singularity"
            self.prompt_singularity_cachedir_creation()
            self.prompt_singularity_cachedir_utilization()
            self.prompt_singularity_cachedir_remote()
            # Nothing meaningful to compress here.
            if not self.tower:
                self.prompt_compression_type()
        except AssertionError as e:
            log.critical(e)
            sys.exit(1)

        summary_log = [
            f"Pipeline revision: '{', '.join(self.revision) if len(self.revision) < 5 else self.revision[0]+',['+str(len(self.revision)-2)+' more revisions],'+self.revision[-1]}'",
            f"Pull containers: '{self.container}'",
        ]
        if self.container == "singularity" and os.environ.get("NXF_SINGULARITY_CACHEDIR") is not None:
            summary_log.append(f"Using [blue]$NXF_SINGULARITY_CACHEDIR[/]': {os.environ['NXF_SINGULARITY_CACHEDIR']}'")
            if self.containers_remote:
                summary_log.append(
                    f"Successfully read {len(self.containers_remote)} containers from the remote '$NXF_SINGULARITY_CACHEDIR' contents."
                )

        # Set an output filename now that we have the outdir
        if self.tower:
            self.output_filename = f"{self.outdir}.git"
            summary_log.append(f"Output file: '{self.output_filename}'")
        elif self.compress_type is not None:
            self.output_filename = f"{self.outdir}.{self.compress_type}"
            summary_log.append(f"Output file: '{self.output_filename}'")
        else:
            summary_log.append(f"Output directory: '{self.outdir}'")

        if not self.tower:
            # Only show entry, if option was prompted.
            summary_log.append(f"Include default institutional configuration: '{self.include_configs}'")
        else:
            summary_log.append(f"Enabled for seqeralabs® Nextflow Tower: '{self.tower}'")

        # Check that the outdir doesn't already exist
        if os.path.exists(self.outdir):
            if not self.force:
                log.error(f"Output directory '{self.outdir}' already exists (use [red]--force[/] to overwrite)")
                sys.exit(1)
            log.warning(f"Deleting existing output directory: '{self.outdir}'")
            shutil.rmtree(self.outdir)

        # Check that compressed output file doesn't already exist
        if self.output_filename and os.path.exists(self.output_filename):
            if not self.force:
                log.error(f"Output file '{self.output_filename}' already exists (use [red]--force[/] to overwrite)")
                sys.exit(1)
            log.warning(f"Deleting existing output file: '{self.output_filename}'")
            os.remove(self.output_filename)

        # Summary log
        log.info("Saving '{}'\n {}".format(self.pipeline, "\n ".join(summary_log)))

        # Perform the actual download
        if self.tower:
            self.download_workflow_tower()
        else:
            self.download_workflow_static()

    def download_workflow_static(self):
        """Downloads a nf-core workflow from GitHub to the local file system in a self-contained manner."""

        # Download the centralised configs first
        if self.include_configs:
            log.info("Downloading centralised configs from GitHub")
            self.download_configs()

        # Download the pipeline files for each selected revision
        log.info("Downloading workflow files from GitHub")

        for item in zip(self.revision, self.wf_sha.values(), self.wf_download_url.values()):
            revision_dirname = self.download_wf_files(revision=item[0], wf_sha=item[1], download_url=item[2])

            if self.include_configs:
                try:
                    self.wf_use_local_configs(revision_dirname)
                except FileNotFoundError as e:
                    log.error("Error editing pipeline config file to use local configs!")
                    log.critical(e)
                    sys.exit(1)

            # Collect all required singularity images
            if self.container == "singularity":
                self.find_container_images(os.path.join(self.outdir, revision_dirname))

                try:
                    self.get_singularity_images(current_revision=item[0])
                except OSError as e:
                    log.critical(f"[red]{e}[/]")
                    sys.exit(1)

        # Compress into an archive
        if self.compress_type is not None:
            log.info("Compressing output into archive")
            self.compress_download()

    def download_workflow_tower(self, location=None):
        """Create a bare-cloned git repository of the workflow, so it can be launched with `tw launch` as file:/ pipeline"""

        log.info("Collecting workflow from GitHub")

        self.workflow_repo = WorkflowRepo(
            remote_url=f"https://github.com/{self.pipeline}.git",
            revision=self.revision if self.revision else None,
            commit=self.wf_sha.values() if bool(self.wf_sha) else None,
            location=location if location else None,  # manual location is required for the tests to work
            in_cache=False,
        )

        # Remove tags for those revisions that had not been selected
        self.workflow_repo.tidy_tags_and_branches()

        # create a bare clone of the modified repository needed for Tower
        self.workflow_repo.bare_clone(os.path.join(self.outdir, self.output_filename))

        # extract the required containers
        if self.container == "singularity":
            for revision, commit in self.wf_sha.items():
                # Checkout the repo in the current revision
                self.workflow_repo.checkout(commit)
                # Collect all required singularity images
                self.find_container_images(self.workflow_repo.access())

                try:
                    self.get_singularity_images(current_revision=revision)
                except OSError as e:
                    log.critical(f"[red]{e}[/]")
                    sys.exit(1)

        # Justify why compression is skipped for Tower downloads (Prompt is not shown, but CLI argument could have been set)
        if self.compress_type is not None:
            log.info("Compression choice is ignored for Tower downloads since nothing can be reasonably compressed.")

    def prompt_pipeline_name(self):
        """Prompt for the pipeline name if not set with a flag"""

        if self.pipeline is None:
            stderr.print("Specify the name of a nf-core pipeline or a GitHub repository name (user/repo).")
            self.pipeline = nf_core.utils.prompt_remote_pipeline_name(self.wfs)

    def prompt_revision(self):
        """
        Prompt for pipeline revision / branch
        Prompt user for revision tag if '--revision' was not set
        If --tower is specified, allow to select multiple revisions
        Also the static download allows for multiple revisions, but
        we do not prompt this option interactively.
        """
        if not bool(self.revision):
            (choice, tag_set) = nf_core.utils.prompt_pipeline_release_branch(
                self.wf_revisions, self.wf_branches, multiple=self.tower
            )
            """
            The checkbox() prompt unfortunately does not support passing a Validator,
            so a user who keeps pressing Enter will flounder past the selection without choice.

            bool(choice), bool(tag_set):
            #############################
            True,  True:  A choice was made and revisions were available.
            False, True:  No selection was made, but revisions were available -> defaults to all available.
            False, False: No selection was made because no revisions were available -> raise AssertionError.
            True,  False: Congratulations, you found a bug! That combo shouldn't happen.
            """

            if bool(choice):
                # have to make sure that self.revision is a list of strings, regardless if choice is str or list of strings.
                self.revision.append(choice) if isinstance(choice, str) else self.revision.extend(choice)
            else:
                if bool(tag_set):
                    self.revision = tag_set
                    log.info("No particular revision was selected, all available will be downloaded.")
                else:
                    raise AssertionError(f"No revisions of {self.pipeline} available for download.")

    def get_revision_hash(self):
        """Find specified revision / branch hash"""

        for revision in self.revision:  # revision is a list of strings, but may be of length 1
            # Branch
            if revision in self.wf_branches.keys():
                self.wf_sha = {**self.wf_sha, revision: self.wf_branches[revision]}

            # Revision
            else:
                for r in self.wf_revisions:
                    if r["tag_name"] == revision:
                        self.wf_sha = {**self.wf_sha, revision: r["tag_sha"]}
                        break

                # Can't find the revisions or branch - throw an error
                else:
                    log.info(
                        "Available {} revisions: '{}'".format(
                            self.pipeline, "', '".join([r["tag_name"] for r in self.wf_revisions])
                        )
                    )
                    log.info("Available {} branches: '{}'".format(self.pipeline, "', '".join(self.wf_branches.keys())))
                    raise AssertionError(f"Not able to find revision / branch '{revision}' for {self.pipeline}")

        # Set the outdir
        if not self.outdir:
            if len(self.wf_sha) > 1:
                self.outdir = f"{self.pipeline.replace('/', '-').lower()}_{datetime.now().strftime('%Y-%m-%d_%H-%M')}"
            else:
                self.outdir = f"{self.pipeline.replace('/', '-').lower()}_{self.revision[0]}"

        if not self.tower:
            for revision, wf_sha in self.wf_sha.items():
                # Set the download URL and return - only applicable for classic downloads
                self.wf_download_url = {
                    **self.wf_download_url,
                    revision: f"https://github.com/{self.pipeline}/archive/{wf_sha}.zip",
                }

    def prompt_config_inclusion(self):
        """Prompt for inclusion of institutional configurations"""
        if stderr.is_interactive:  # Use rich auto-detection of interactive shells
            self.include_configs = questionary.confirm(
                "Include the nf-core's default institutional configuration files into the download?",
                style=nf_core.utils.nfcore_question_style,
            ).ask()
        else:
            self.include_configs = False
            # do not include by default.

    def prompt_container_download(self):
        """Prompt whether to download container images or not"""

        if self.container is None and stderr.is_interactive and not self.tower:
            stderr.print("\nIn addition to the pipeline code, this tool can download software containers.")
            self.container = questionary.select(
                "Download software container images:",
                choices=["none", "singularity"],
                style=nf_core.utils.nfcore_question_style,
            ).unsafe_ask()

    def prompt_singularity_cachedir_creation(self):
        """Prompt about using $NXF_SINGULARITY_CACHEDIR if not already set"""
        if (
            self.container == "singularity"
            and os.environ.get("NXF_SINGULARITY_CACHEDIR") is None
            and stderr.is_interactive  # Use rich auto-detection of interactive shells
        ):
            stderr.print(
                "\nNextflow and nf-core can use an environment variable called [blue]$NXF_SINGULARITY_CACHEDIR[/] that is a path to a directory where remote Singularity images are stored. "
                "This allows downloaded images to be cached in a central location."
            )
            if rich.prompt.Confirm.ask(
                "[blue bold]?[/] [bold]Define [blue not bold]$NXF_SINGULARITY_CACHEDIR[/] for a shared Singularity image download folder?[/]"
            ):
                if not self.singularity_cache_index:
                    self.singularity_cache == "amend"  # retain "remote" choice.
                # Prompt user for a cache directory path
                cachedir_path = None
                while cachedir_path is None:
                    prompt_cachedir_path = questionary.path(
                        "Specify the path:", only_directories=True, style=nf_core.utils.nfcore_question_style
                    ).unsafe_ask()
                    cachedir_path = os.path.abspath(os.path.expanduser(prompt_cachedir_path))
                    if prompt_cachedir_path == "":
                        log.error("Not using [blue]$NXF_SINGULARITY_CACHEDIR[/]")
                        cachedir_path = False
                    elif not os.path.isdir(cachedir_path):
                        log.error(f"'{cachedir_path}' is not a directory.")
                        cachedir_path = None
                if cachedir_path:
                    os.environ["NXF_SINGULARITY_CACHEDIR"] = cachedir_path

                    """
                    Optionally, create a permanent entry for the NXF_SINGULARITY_CACHEDIR in the terminal profile.
                    Currently support for bash and zsh.
                    ToDo: "sh", "dash", "ash","csh", "tcsh", "ksh", "fish", "cmd", "powershell", "pwsh"?
                    """

                    if os.getenv("SHELL", "") == "/bin/bash":
                        shellprofile_path = os.path.expanduser("~/~/.bash_profile")
                        if not os.path.isfile(shellprofile_path):
                            shellprofile_path = os.path.expanduser("~/.bashrc")
                            if not os.path.isfile(shellprofile_path):
                                shellprofile_path = False
                    elif os.getenv("SHELL", "") == "/bin/zsh":
                        shellprofile_path = os.path.expanduser("~/.zprofile")
                        if not os.path.isfile(shellprofile_path):
                            shellprofile_path = os.path.expanduser("~/.zshenv")
                            if not os.path.isfile(shellprofile_path):
                                shellprofile_path = False
                    else:
                        shellprofile_path = os.path.expanduser("~/.profile")
                        if not os.path.isfile(shellprofile_path):
                            shellprofile_path = False

                    if shellprofile_path:
                        stderr.print(
                            f"\nSo that [blue]$NXF_SINGULARITY_CACHEDIR[/] is always defined, you can add it to your [blue not bold]~/{os.path.basename(shellprofile_path)}[/] file ."
                            "This will then be automatically set every time you open a new terminal. We can add the following line to this file for you: \n"
                            f'[blue]export NXF_SINGULARITY_CACHEDIR="{cachedir_path}"[/]'
                        )
                        append_to_file = rich.prompt.Confirm.ask(
                            f"[blue bold]?[/] [bold]Add to [blue not bold]~/{os.path.basename(shellprofile_path)}[/] ?[/]"
                        )
                        if append_to_file:
                            with open(os.path.expanduser(shellprofile_path), "a") as f:
                                f.write(
                                    "\n\n#######################################\n"
                                    f"## Added by `nf-core download` v{nf_core.__version__} ##\n"
                                    + f'export NXF_SINGULARITY_CACHEDIR="{cachedir_path}"'
                                    + "\n#######################################\n"
                                )
                            log.info(f"Successfully wrote to [blue]{shellprofile_path}[/]")
                            log.warning(
                                "You will need reload your terminal after the download completes for this to take effect."
                            )

    def prompt_singularity_cachedir_utilization(self):
        """Ask if we should *only* use $NXF_SINGULARITY_CACHEDIR without copying into target"""
        if (
            self.singularity_cache is None  # no choice regarding singularity cache has been made.
            and self.container == "singularity"
            and os.environ.get("NXF_SINGULARITY_CACHEDIR") is not None
            and stderr.is_interactive
        ):
            stderr.print(
                "\nIf you are working on the same system where you will run Nextflow, you can amend the downloaded images to the ones in the"
                "[blue not bold]$NXF_SINGULARITY_CACHEDIR[/] folder, Nextflow will automatically find them."
                "However if you will transfer the downloaded files to a different system then they should be copied to the target folder."
            )
            self.singularity_cache = questionary.select(
                "Copy singularity images from $NXF_SINGULARITY_CACHEDIR to the target folder or amend new images to the cache?",
                choices=["amend", "copy"],
                style=nf_core.utils.nfcore_question_style,
            ).unsafe_ask()

    def prompt_singularity_cachedir_remote(self):
        """Prompt about the index of a remote $NXF_SINGULARITY_CACHEDIR"""
        if (
            self.container == "singularity"
            and self.singularity_cache == "remote"
            and self.singularity_cache_index is None
            and stderr.is_interactive  # Use rich auto-detection of interactive shells
        ):
            # Prompt user for a file listing the contents of the remote cache directory
            cachedir_index = None
            while cachedir_index is None:
                prompt_cachedir_index = questionary.path(
                    "Specify a list of the container images that are already present on the remote system:",
                    validate=SingularityCacheFilePathValidator,
                    style=nf_core.utils.nfcore_question_style,
                ).unsafe_ask()
                cachedir_index = os.path.abspath(os.path.expanduser(prompt_cachedir_index))
                if prompt_cachedir_index == "":
                    log.error("Will disregard contents of a remote [blue]$NXF_SINGULARITY_CACHEDIR[/]")
                    self.singularity_cache_index = None
                    self.singularity_cache = "copy"
                elif not os.access(cachedir_index, os.R_OK):
                    log.error(f"'{cachedir_index}' is not a readable file.")
                    cachedir_index = None
            if cachedir_index:
                self.singularity_cache_index = cachedir_index
        # in any case read the remote containers, even if no prompt was shown.
        self.read_remote_containers()

    def read_remote_containers(self):
        """Reads the file specified as index for the remote Singularity cache dir"""
        if (
            self.container == "singularity"
            and self.singularity_cache == "remote"
            and self.singularity_cache_index is not None
        ):
            n_total_images = 0
            try:
                with open(self.singularity_cache_index) as indexfile:
                    for line in indexfile.readlines():
                        match = re.search(r"([^\/\\]+\.img)", line, re.S)
                        if match:
                            n_total_images += 1
                            self.containers_remote.append(match.group(0))
                    if n_total_images == 0:
                        raise LookupError("Could not find valid container names in the index file.")
                    self.containers_remote = sorted(list(set(self.containers_remote)))
            except (FileNotFoundError, LookupError) as e:
                log.error(f"[red]Issue with reading the specified remote $NXF_SINGULARITY_CACHE index:[/]\n{e}\n")
                if stderr.is_interactive and rich.prompt.Confirm.ask(f"[blue]Specify a new index file and try again?"):
                    self.singularity_cache_index = None  # reset chosen path to index file.
                    self.prompt_singularity_cachedir_remote()
                else:
                    log.info("Proceeding without consideration of the remote $NXF_SINGULARITY_CACHE index.")
                    self.singularity_cache_index = None
                    if os.environ.get("NXF_SINGULARITY_CACHEDIR"):
                        self.singularity_cache = "copy"  # default to copy if possible, otherwise skip.
                    else:
                        self.singularity_cache = None

    def prompt_compression_type(self):
        """Ask user if we should compress the downloaded files"""
        if self.compress_type is None:
            stderr.print(
                "\nIf transferring the downloaded files to another system, it can be convenient to have everything compressed in a single file."
            )
            if self.container == "singularity":
                stderr.print(
                    "[bold]This is [italic]not[/] recommended when downloading Singularity images, as it can take a long time and saves very little space."
                )
            self.compress_type = questionary.select(
                "Choose compression type:",
                choices=[
                    "none",
                    "tar.gz",
                    "tar.bz2",
                    "zip",
                ],
                style=nf_core.utils.nfcore_question_style,
            ).unsafe_ask()

        # Correct type for no-compression
        if self.compress_type == "none":
            self.compress_type = None

    def download_wf_files(self, revision, wf_sha, download_url):
        """Downloads workflow files from GitHub to the :attr:`self.outdir`."""
        log.debug(f"Downloading {download_url}")

        # Download GitHub zip file into memory and extract
        url = requests.get(download_url)
        with ZipFile(io.BytesIO(url.content)) as zipfile:
            zipfile.extractall(self.outdir)

        # create a filesystem-safe version of the revision name for the directory
        revision_dirname = re.sub("[^0-9a-zA-Z]+", "_", revision)
        # account for name collisions, if there is a branch / release named "configs" or "singularity-images"
        if revision_dirname in ["configs", "singularity-images"]:
            revision_dirname = re.sub("[^0-9a-zA-Z]+", "_", self.pipeline + revision_dirname)

        # Rename the internal directory name to be more friendly
        gh_name = f"{self.pipeline}-{wf_sha if bool(wf_sha) else ''}".split("/")[-1]
        os.rename(os.path.join(self.outdir, gh_name), os.path.join(self.outdir, revision_dirname))

        # Make downloaded files executable
        for dirpath, _, filelist in os.walk(os.path.join(self.outdir, revision_dirname)):
            for fname in filelist:
                os.chmod(os.path.join(dirpath, fname), 0o775)

        return revision_dirname

    def download_configs(self):
        """Downloads the centralised config profiles from nf-core/configs to :attr:`self.outdir`."""
        configs_zip_url = "https://github.com/nf-core/configs/archive/master.zip"
        configs_local_dir = "configs-master"
        log.debug(f"Downloading {configs_zip_url}")

        # Download GitHub zip file into memory and extract
        url = requests.get(configs_zip_url)
        with ZipFile(io.BytesIO(url.content)) as zipfile:
            zipfile.extractall(self.outdir)

        # Rename the internal directory name to be more friendly
        os.rename(os.path.join(self.outdir, configs_local_dir), os.path.join(self.outdir, "configs"))

        # Make downloaded files executable
        for dirpath, _, filelist in os.walk(os.path.join(self.outdir, "configs")):
            for fname in filelist:
                os.chmod(os.path.join(dirpath, fname), 0o775)

    def wf_use_local_configs(self, revision_dirname):
        """Edit the downloaded nextflow.config file to use the local config files"""
        nfconfig_fn = os.path.join(self.outdir, revision_dirname, "nextflow.config")
        find_str = "https://raw.githubusercontent.com/nf-core/configs/${params.custom_config_version}"
        repl_str = "${projectDir}/../configs/"
        log.debug(f"Editing 'params.custom_config_base' in '{nfconfig_fn}'")

        # Load the nextflow.config file into memory
        with open(nfconfig_fn, "r") as nfconfig_fh:
            nfconfig = nfconfig_fh.read()

        # Replace the target string
        log.debug(f"Replacing '{find_str}' with '{repl_str}'")
        nfconfig = nfconfig.replace(find_str, repl_str)

        # Append the singularity.cacheDir to the end if we need it
        if self.container == "singularity" and self.singularity_cache == "copy":
            nfconfig += (
                f"\n\n// Added by `nf-core download` v{nf_core.__version__} //\n"
                + 'singularity.cacheDir = "${projectDir}/../singularity-images/"'
                + "\n///////////////////////////////////////"
            )

        # Write the file out again
        log.debug(f"Updating '{nfconfig_fn}'")
        with open(nfconfig_fn, "w") as nfconfig_fh:
            nfconfig_fh.write(nfconfig)

    def find_container_images(self, workflow_directory):
        """Find container image names for workflow.

        Starts by using `nextflow config` to pull out any process.container
        declarations. This works for DSL1. It should return a simple string with resolved logic.

        Second, we look for DSL2 containers. These can't be found with
        `nextflow config` at the time of writing, so we scrape the pipeline files.
        This returns raw source code that will likely need to be cleaned.

        If multiple containers are found, prioritise any prefixed with http for direct download.

        Example syntax:

        Early DSL2:
            if (workflow.containerEngine == 'singularity' && !params.singularity_pull_docker_container) {
                container "https://depot.galaxyproject.org/singularity/fastqc:0.11.9--0"
            } else {
                container "quay.io/biocontainers/fastqc:0.11.9--0"
            }

        Later DSL2:
            container "${ workflow.containerEngine == 'singularity' && !task.ext.singularity_pull_docker_container ?
                'https://depot.galaxyproject.org/singularity/fastqc:0.11.9--0' :
                'biocontainers/fastqc:0.11.9--0' }"

        Later DSL2, variable is being used:
            container "${ workflow.containerEngine == 'singularity' && !task.ext.singularity_pull_docker_container ?
                "https://depot.galaxyproject.org/singularity/${container_id}" :
                "quay.io/biocontainers/${container_id}" }"

            container_id = 'mulled-v2-1fa26d1ce03c295fe2fdcf85831a92fbcbd7e8c2:afaaa4c6f5b308b4b6aa2dd8e99e1466b2a6b0cd-0'

        DSL1 / Special case DSL2:
            container "nfcore/cellranger:6.0.2"
        """

        log.debug("Fetching container names for workflow")
        # since this is run for multiple revisions now, account for previously detected containers.
        containers_raw = [] if not self.containers else self.containers

        # Use linting code to parse the pipeline nextflow config
        self.nf_config = nf_core.utils.fetch_wf_config(workflow_directory)

        # Find any config variables that look like a container
        for k, v in self.nf_config.items():
            if k.startswith("process.") and k.endswith(".container"):
                containers_raw.append(v.strip('"').strip("'"))

        # Recursive search through any DSL2 module files for container spec lines.
        for subdir, _, files in os.walk(os.path.join(workflow_directory, "modules")):
            for file in files:
                if file.endswith(".nf"):
                    file_path = os.path.join(subdir, file)
                    with open(file_path, "r") as fh:
                        # Look for any lines with `container "xxx"`
                        this_container = None
                        contents = fh.read()
                        matches = re.findall(r"container\s*\"([^\"]*)\"", contents, re.S)
                        if matches:
                            for match in matches:
                                # Look for a http download URL.
                                # Thanks Stack Overflow for the regex: https://stackoverflow.com/a/3809435/713980
                                url_regex = r"https?:\/\/(www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b([-a-zA-Z0-9()@:%_\+.~#?&//=]*)"
                                url_match = re.search(url_regex, match, re.S)
                                if url_match:
                                    this_container = url_match.group(0)
                                    break  # Prioritise http, exit loop as soon as we find it

                                # No https download, is the entire container string a docker URI?
                                # Thanks Stack Overflow for the regex: https://stackoverflow.com/a/39672069/713980
                                docker_regex = r"^(?:(?=[^:\/]{1,253})(?!-)[a-zA-Z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-zA-Z0-9-]{1,63}(?<!-))*(?::[0-9]{1,5})?/)?((?![._-])(?:[a-z0-9._-]*)(?<![._-])(?:/(?![._-])[a-z0-9._-]*(?<![._-]))*)(?::(?![.-])[a-zA-Z0-9_.-]{1,128})?$"
                                docker_match = re.match(docker_regex, match.strip(), re.S)
                                if docker_match:
                                    this_container = docker_match.group(0)
                                    break

                                """
                                Some modules declare the container as separate variable. This entails that " instead of ' is used,
                                so the above regex will match, but end prematurely before the container name is captured.

                                Therefore, we need to repeat the search over the contents, extract the variable name, and use it inside a new regex.

                                To get the variable name ( ${container_id} in above example ), we match the literal word "container" and use lookbehind (reset the match).
                                Then we skip [^${}]+ everything that is not $ or curly braces. The next capture group is
                                ${ followed by any characters that are not curly braces [^{}]+ and ended by a closing curly brace (}),
                                but only if it's not followed by any other curly braces (?![^{]*}). The latter ensures we capture the innermost
                                variable name.
                                """
                                container_definition = re.search(
                                    r"(?<=container)[^\${}]+\${([^{}]+)}(?![^{]*})", contents
                                )

                                if bool(container_definition) and bool(container_definition.group(1)):
                                    pattern = re.escape(container_definition.group(1))
                                    # extract the quoted string(s) following the variable assignment
                                    container_names = re.findall(r"%s\s*=\s*[\"\']([^\"\']+)[\"\']" % pattern, contents)

                                    if bool(container_names):
                                        if isinstance(container_names, str):
                                            this_container = (
                                                f"https://depot.galaxyproject.org/singularity/{container_names}"
                                            )
                                            break
                                        elif isinstance(container_names, list):
                                            for container_name in container_names:
                                                containers_raw.append(
                                                    f"https://depot.galaxyproject.org/singularity/{container_name}"
                                                )
                                        else:
                                            # didn't find valid container declaration, but parsing succeeded.
                                            this_container = None

                                    break  # break the loop like for url_match and docker_match
                                else:  # giving up
                                    log.error(
                                        f"[red]Cannot parse container string in '{file_path}':\n\n{textwrap.indent(match, '    ')}\n\n:warning: Skipping this singularity image.."
                                    )

                        if this_container:
                            containers_raw.append(this_container)

        # Remove duplicates and sort
        self.containers = sorted(list(set(containers_raw)))

    def get_singularity_images(self, current_revision=""):
        """Loop through container names and download Singularity images"""

        if len(self.containers) == 0:
            log.info("No container names found in workflow")
        else:
            log.info(
                f"Processing workflow revision {current_revision}, found {len(self.containers)} container image{'s' if len(self.containers) > 1 else ''} in total."
            )

            with DownloadProgress() as progress:
                task = progress.add_task(
                    "Collecting container images", total=len(self.containers), progress_type="summary"
                )

                # Organise containers based on what we need to do with them
                containers_exist = []
                containers_cache = []
                containers_download = []
                containers_pull = []
                for container in self.containers:
                    # Fetch the output and cached filenames for this container
                    out_path, cache_path = self.singularity_image_filenames(container)

                    # Check that the directories exist
                    out_path_dir = os.path.dirname(out_path)
                    if not os.path.isdir(out_path_dir):
                        log.debug(f"Output directory not found, creating: {out_path_dir}")
                        os.makedirs(out_path_dir)
                    if cache_path:
                        cache_path_dir = os.path.dirname(cache_path)
                        if not os.path.isdir(cache_path_dir):
                            log.debug(f"Cache directory not found, creating: {cache_path_dir}")
                            os.makedirs(cache_path_dir)

                    # We already have the target file in place or in remote cache, return
                    if os.path.exists(out_path) or os.path.basename(out_path) in self.containers_remote:
                        containers_exist.append(container)
                        continue

                    # We have a copy of this in the NXF_SINGULARITY_CACHE dir
                    if cache_path and os.path.exists(cache_path):
                        containers_cache.append([container, out_path, cache_path])
                        continue

                    # Direct download within Python
                    if container.startswith("http"):
                        containers_download.append([container, out_path, cache_path])
                        continue

                    # Pull using singularity
                    containers_pull.append([container, out_path, cache_path])

                # Exit if we need to pull images and Singularity is not installed
                if len(containers_pull) > 0:
                    if not (shutil.which("singularity") or shutil.which("apptainer")):
                        raise OSError(
                            "Singularity/Apptainer is needed to pull images, but it is not installed or not in $PATH"
                        )

                if containers_exist:
                    if self.singularity_cache_index is not None:
                        log.info(
                            f"{len(containers_exist)} containers are already cached remotely and won't be retrieved."
                        )
                    # Go through each method of fetching containers in order
                    for container in containers_exist:
                        progress.update(task, description="Image file exists at destination")
                        progress.update(task, advance=1)

                if containers_cache:
                    for container in containers_cache:
                        progress.update(task, description="Copying singularity images from cache")
                        self.singularity_copy_cache_image(*container)
                        progress.update(task, advance=1)

                if containers_download or containers_pull:
                    # if clause gives slightly better UX, because Download is no longer displayed if nothing is left to be downloaded.
                    with concurrent.futures.ThreadPoolExecutor(max_workers=self.parallel_downloads) as pool:
                        progress.update(task, description="Downloading singularity images")

                        # Kick off concurrent downloads
                        future_downloads = [
                            pool.submit(self.singularity_download_image, *container, progress)
                            for container in containers_download
                        ]

                        # Make ctrl-c work with multi-threading
                        self.kill_with_fire = False

                        try:
                            # Iterate over each threaded download, waiting for them to finish
                            for future in concurrent.futures.as_completed(future_downloads):
                                future.result()
                                try:
                                    progress.update(task, advance=1)
                                except Exception as e:
                                    log.error(f"Error updating progress bar: {e}")

                        except KeyboardInterrupt:
                            # Cancel the future threads that haven't started yet
                            for future in future_downloads:
                                future.cancel()
                            # Set the variable that the threaded function looks for
                            # Will trigger an exception from each thread
                            self.kill_with_fire = True
                            # Re-raise exception on the main thread
                            raise

                    for container in containers_pull:
                        progress.update(task, description="Pulling singularity images")
                        try:
                            self.singularity_pull_image(*container, progress)
                        except RuntimeWarning as r:
                            # Raise exception if this is not possible
                            log.error("Not able to pull image. Service might be down or internet connection is dead.")
                            raise r
                        finally:
                            progress.update(task, advance=1)

    def singularity_image_filenames(self, container):
        """Check Singularity cache for image, copy to destination folder if found.

        Args:
            container (str):    A pipeline's container name. Can be direct download URL
                                or a Docker Hub repository ID.

        Returns:
            results (bool, str):    Returns True if we have the image in the target location.
                                    Returns a download path if not.
        """

        # Generate file paths
        # Based on simpleName() function in Nextflow code:
        # https://github.com/nextflow-io/nextflow/blob/671ae6d85df44f906747c16f6d73208dbc402d49/modules/nextflow/src/main/groovy/nextflow/container/SingularityCache.groovy#L69-L94
        out_name = container
        # Strip URI prefix
        out_name = re.sub(r"^.*:\/\/", "", out_name)
        # Detect file extension
        extension = ".img"
        if ".sif:" in out_name:
            extension = ".sif"
            out_name = out_name.replace(".sif:", "-")
        elif out_name.endswith(".sif"):
            extension = ".sif"
            out_name = out_name[:-4]
        # Strip : and / characters
        out_name = out_name.replace("/", "-").replace(":", "-")
        # Add file extension
        out_name = out_name + extension

        # Full destination and cache paths
        out_path = os.path.abspath(os.path.join(self.outdir, "singularity-images", out_name))
        cache_path = None
        if os.environ.get("NXF_SINGULARITY_CACHEDIR"):
            cache_path = os.path.join(os.environ["NXF_SINGULARITY_CACHEDIR"], out_name)
            # Use only the cache - set this as the main output path
            if self.singularity_cache == "amend":
                out_path = cache_path
                cache_path = None
        elif self.singularity_cache in ["amend", "copy"]:
            raise FileNotFoundError("Singularity cache is required but no '$NXF_SINGULARITY_CACHEDIR' set!")

        return (out_path, cache_path)

    def singularity_copy_cache_image(self, container, out_path, cache_path):
        """Copy Singularity image from NXF_SINGULARITY_CACHEDIR to target folder."""
        # Copy to destination folder if we have a cached version
        if cache_path and os.path.exists(cache_path):
            log.debug(f"Copying {container} from cache: '{os.path.basename(out_path)}'")
            shutil.copyfile(cache_path, out_path)

    def singularity_download_image(self, container, out_path, cache_path, progress):
        """Download a singularity image from the web.

        Use native Python to download the file.

        Args:
            container (str): A pipeline's container name. Usually it is of similar format
                to ``https://depot.galaxyproject.org/singularity/name:version``
            out_path (str): The final target output path
            cache_path (str, None): The NXF_SINGULARITY_CACHEDIR path if set, None if not
            progress (Progress): Rich progress bar instance to add tasks to.
        """
        log.debug(f"Downloading Singularity image: '{container}'")

        # Set output path to save file to
        output_path = cache_path or out_path
        output_path_tmp = f"{output_path}.partial"
        log.debug(f"Downloading to: '{output_path_tmp}'")

        # Set up progress bar
        nice_name = container.split("/")[-1][:50]
        task = progress.add_task(nice_name, start=False, total=False, progress_type="download")
        try:
            # Delete temporary file if it already exists
            if os.path.exists(output_path_tmp):
                os.remove(output_path_tmp)

            # Open file handle and download
            with open(output_path_tmp, "wb") as fh:
                # Disable caching as this breaks streamed downloads
                with requests_cache.disabled():
                    r = requests.get(container, allow_redirects=True, stream=True, timeout=60 * 5)
                    filesize = r.headers.get("Content-length")
                    if filesize:
                        progress.update(task, total=int(filesize))
                        progress.start_task(task)

                    # Stream download
                    for data in r.iter_content(chunk_size=io.DEFAULT_BUFFER_SIZE):
                        # Check that the user didn't hit ctrl-c
                        if self.kill_with_fire:
                            raise KeyboardInterrupt
                        progress.update(task, advance=len(data))
                        fh.write(data)

            # Rename partial filename to final filename
            os.rename(output_path_tmp, output_path)
            output_path_tmp = None

            # Copy cached download if we are using the cache
            if cache_path:
                log.debug(f"Copying {container} from cache: '{os.path.basename(out_path)}'")
                progress.update(task, description="Copying from cache to target directory")
                shutil.copyfile(cache_path, out_path)

            progress.remove_task(task)

        except:
            # Kill the progress bars
            for t in progress.task_ids:
                progress.remove_task(t)
            # Try to delete the incomplete download
            log.debug(f"Deleting incompleted singularity image download:\n'{output_path_tmp}'")
            if output_path_tmp and os.path.exists(output_path_tmp):
                os.remove(output_path_tmp)
            if output_path and os.path.exists(output_path):
                os.remove(output_path)
            # Re-raise the caught exception
            raise

    def singularity_pull_image(self, container, out_path, cache_path, progress):
        """Pull a singularity image using ``singularity pull``

        Attempt to use a local installation of singularity to pull the image.

        Args:
            container (str): A pipeline's container name. Usually it is of similar format
                to ``nfcore/name:version``.

        Raises:
            Various exceptions possible from `subprocess` execution of Singularity.
        """
        output_path = cache_path or out_path

        # Pull using singularity
        address = f"docker://{container.replace('docker://', '')}"
        if shutil.which("singularity"):
            singularity_command = ["singularity", "pull", "--name", output_path, address]
        elif shutil.which("apptainer"):
            singularity_command = ["apptainer", "pull", "--name", output_path, address]
        else:
            raise OSError("Singularity/Apptainer is needed to pull images, but it is not installed or not in $PATH")
        log.debug(f"Building singularity image: {address}")
        log.debug(f"Singularity command: {' '.join(singularity_command)}")

        # Progress bar to show that something is happening
        task = progress.add_task(container, start=False, total=False, progress_type="singularity_pull", current_log="")

        # Run the singularity pull command
        with subprocess.Popen(
            singularity_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        ) as proc:
            lines = []
            for line in proc.stdout:
                lines.append(line)
                progress.update(task, current_log=line.strip())

        if lines:
            # something went wrong with the container retrieval
            if any("FATAL: " in line for line in lines):
                log.error(f'[bold red]The singularity image "{container}" could not be pulled:[/]\n\n{"".join(lines)}')
                log.error(
                    f'Skipping failed pull of "{container}". Please troubleshoot the command \n"{" ".join(singularity_command)}"\n\n\n'
                )
                progress.remove_task(task)
                return

        # Copy cached download if we are using the cache
        if cache_path:
            log.debug(f"Copying {container} from cache: '{os.path.basename(out_path)}'")
            progress.update(task, current_log="Copying from cache to target directory")
            shutil.copyfile(cache_path, out_path)

        progress.remove_task(task)

    def compress_download(self):
        """Take the downloaded files and make a compressed .tar.gz archive."""
        log.debug(f"Creating archive: {self.output_filename}")

        # .tar.gz and .tar.bz2 files
        if self.compress_type in ["tar.gz", "tar.bz2"]:
            ctype = self.compress_type.split(".")[1]
            with tarfile.open(self.output_filename, f"w:{ctype}") as tar:
                tar.add(self.outdir, arcname=os.path.basename(self.outdir))
            tar_flags = "xzf" if ctype == "gz" else "xjf"
            log.info(f"Command to extract files: [bright_magenta]tar -{tar_flags} {self.output_filename}[/]")

        # .zip files
        if self.compress_type == "zip":
            with ZipFile(self.output_filename, "w") as zip_file:
                # Iterate over all the files in directory
                for folder_name, _, filenames in os.walk(self.outdir):
                    for filename in filenames:
                        # create complete filepath of file in directory
                        file_path = os.path.join(folder_name, filename)
                        # Add file to zip
                        zip_file.write(file_path)
            log.info(f"Command to extract files: [bright_magenta]unzip {self.output_filename}[/]")

        # Delete original files
        log.debug(f"Deleting uncompressed files: '{self.outdir}'")
        shutil.rmtree(self.outdir)

        # Calculate md5sum for output file
        log.info(f"MD5 checksum for '{self.output_filename}': [blue]{nf_core.utils.file_md5(self.output_filename)}[/]")


class WorkflowRepo(SyncedRepo):
    """
    An object to store details about a locally cached workflow repository.

    Important Attributes:
        fullname: The full name of the repository, ``nf-core/{self.pipelinename}``.
        local_repo_dir (str): The local directory, where the workflow is cloned into. Defaults to ``$HOME/.cache/nf-core/nf-core/{self.pipeline}``.

    """

    def __init__(
        self,
        remote_url,
        revision,
        commit,
        location=None,
        hide_progress=False,
        in_cache=True,
    ):
        """
        Initializes the object and clones the workflows git repository if it is not already present

        Args:
            remote_url (str): The URL of the remote repository. Defaults to None.
            self.revision (list of str): The revisions to include. A list of strings.
            commits (dict of str): The checksums to linked with the revisions.
            no_pull (bool, optional): Whether to skip the pull step. Defaults to False.
            hide_progress (bool, optional): Whether to hide the progress bar. Defaults to False.
            in_cache (bool, optional): Whether to clone the repository from the cache. Defaults to False.
        """
        self.remote_url = remote_url
        if isinstance(revision, str):
            self.revision = [revision]
        elif isinstance(revision, list):
            self.revision = [*revision]
        else:
            self.revision = []
        if isinstance(commit, str):
            self.commit = [commit]
        elif isinstance(commit, list):
            self.commit = [*commit]
        else:
            self.commit = []
        self.fullname = nf_core.modules.modules_utils.repo_full_name_from_remote(self.remote_url)
        self.retries = 0  # retries for setting up the locally cached repository
        self.hide_progress = hide_progress

        self.setup_local_repo(remote=remote_url, location=location, in_cache=in_cache)

        # expose some instance attributes
        self.tags = self.repo.tags

    def __repr__(self):
        """Called by print, creates representation of object"""
        return f"<Locally cached repository: {self.fullname}, revisions {', '.join(self.revision)}\n cached at: {self.local_repo_dir}>"

    def access(self):
        if os.path.exists(self.local_repo_dir):
            return self.local_repo_dir
        else:
            return None

    def checkout(self, commit):
        return super().checkout(commit)

    def get_remote_branches(self, remote_url):
        return super().get_remote_branches(remote_url)

    def retry_setup_local_repo(self, skip_confirm=False):
        self.retries += 1
        if skip_confirm or rich.prompt.Confirm.ask(
            f"[violet]Delete local cache '{self.local_repo_dir}' and try again?"
        ):
            if (
                self.retries > 1
            ):  # One unconfirmed retry is acceptable, but prevent infinite loops without user interaction.
                log.error(
                    f"Errors with locally cached repository of '{self.fullname}'. Please delete '{self.local_repo_dir}' manually and try again."
                )
                sys.exit(1)
            if not skip_confirm:  # Feedback to user for manual confirmation.
                log.info(f"Removing '{self.local_repo_dir}'")
            shutil.rmtree(self.local_repo_dir)
            self.setup_local_repo(self.remote_url, in_cache=False)
        else:
            raise LookupError("Exiting due to error with locally cached Git repository.")

    def setup_local_repo(self, remote, location=None, in_cache=True):
        """
        Sets up the local git repository. If the repository has been cloned previously, it
        returns a git.Repo object of that clone. Otherwise it tries to clone the repository from
        the provided remote URL and returns a git.Repo of the new clone.

        Args:
            remote (str): git url of remote
            location (Path): location where the clone should be created/cached.
            in_cache (bool, optional): Whether to clone the repository from the cache. Defaults to False.
        Sets self.repo
        """
        if location:
            self.local_repo_dir = os.path.join(location, self.fullname)
        else:
            self.local_repo_dir = os.path.join(NFCORE_DIR if not in_cache else NFCORE_CACHE_DIR, self.fullname)

        try:
            if not os.path.exists(self.local_repo_dir):
                try:
                    pbar = rich.progress.Progress(
                        "[bold blue]{task.description}",
                        rich.progress.BarColumn(bar_width=None),
                        "[bold yellow]{task.fields[state]}",
                        transient=True,
                        disable=os.environ.get("HIDE_PROGRESS", None) is not None or self.hide_progress,
                    )
                    with pbar:
                        self.repo = git.Repo.clone_from(
                            remote,
                            self.local_repo_dir,
                            progress=RemoteProgressbar(pbar, self.fullname, self.remote_url, "Cloning"),
                        )
                    super().update_local_repo_status(self.fullname, True)
                except GitCommandError:
                    raise LookupError(f"Failed to clone from the remote: `{remote}`")
            else:
                self.repo = git.Repo(self.local_repo_dir)

                if super().no_pull_global:
                    super().update_local_repo_status(self.fullname, True)
                # If the repo is already cloned, fetch the latest changes from the remote
                if not super().local_repo_synced(self.fullname):
                    pbar = rich.progress.Progress(
                        "[bold blue]{task.description}",
                        rich.progress.BarColumn(bar_width=None),
                        "[bold yellow]{task.fields[state]}",
                        transient=True,
                        disable=os.environ.get("HIDE_PROGRESS", None) is not None or self.hide_progress,
                    )
                    with pbar:
                        self.repo.remotes.origin.fetch(
                            progress=RemoteProgressbar(pbar, self.fullname, self.remote_url, "Pulling")
                        )
                    super().update_local_repo_status(self.fullname, True)

        except (GitCommandError, InvalidGitRepositoryError) as e:
            log.error(f"[red]Could not set up local cache of modules repository:[/]\n{e}\n")
            self.retry_setup_local_repo()

    def tidy_tags_and_branches(self):
        """
        Function to delete all tags and branches that are not of interest to the downloader.
        This allows a clutter-free experience in Tower. The untagged commits are evidently still available.

        However, due to local caching, the downloader might also want access to revisions that had been deleted before.
        In that case, don't bother with re-adding the tags and rather download  anew from Github.
        """
        if self.revision and self.repo and self.repo.tags:
            # create a set to keep track of the revisions to process & check
            desired_revisions = set(self.revision)

            # determine what needs pruning
            tags_to_remove = {tag for tag in self.repo.tags if tag.name not in desired_revisions.union({"latest"})}
            heads_to_remove = {head for head in self.repo.heads if head.name not in desired_revisions.union({"latest"})}

            try:
                # delete unwanted tags from repository
                for tag in tags_to_remove:
                    self.repo.delete_tag(tag)
                self.tags = self.repo.tags

                # switch to a revision that should be kept, because deleting heads fails, if they are checked out (e.g. "master")
                self.checkout(self.revision[0])

                # delete unwanted heads/branches from repository
                for head in heads_to_remove:
                    self.repo.delete_head(head)

                # ensure all desired revisions/branches are available
                for revision in desired_revisions:
                    if not self.repo.is_valid_object(revision):
                        self.checkout(revision)
                        self.repo.create_head(revision, revision)
                        if self.repo.head.is_detached:
                            self.repo.head.reset(index=True, working_tree=True)

                # no branch exists, but one is required for Tower's UI to display revisions correctly). Thus, "latest" will be created.
                if not bool(self.repo.heads):
                    if self.repo.is_valid_object("latest"):
                        # "latest" exists as tag but not as branch
                        self.repo.create_head("latest", "latest")  # create a new head for latest
                        self.checkout("latest")
                    else:
                        # desired revisions may contain arbitrary branch names that do not correspond to valid sematic versioning patterns.
                        valid_versions = [
                            VersionParser(v)
                            for v in desired_revisions
                            if re.match(r"\d+\.\d+(?:\.\d+)*(?:[\w\-_])*", v)
                        ]
                        # valid versions sorted in ascending order, last will be aliased as "latest".
                        latest = sorted(valid_versions)[-1]
                        self.repo.create_head("latest", latest)
                        self.checkout(latest)
                    if self.repo.head.is_detached:
                        self.repo.head.reset(index=True, working_tree=True)

                self.heads = self.repo.heads

                # get all tags and available remote_branches
                completed_revisions = {revision.name for revision in self.repo.heads + self.repo.tags}

                # verify that all requested revisions are available.
                # a local cache might lack revisions that were deleted during a less comprehensive previous download.
                if bool(desired_revisions - completed_revisions):
                    log.info(
                        f"Locally cached version of the pipeline lacks selected revisions {', '.join(desired_revisions - completed_revisions)}. Downloading anew from GitHub..."
                    )
                    self.retry_setup_local_repo(skip_confirm=True)
                    self.tidy_tags_and_branches()
            except (GitCommandError, InvalidGitRepositoryError) as e:
                log.error(f"[red]Adapting your pipeline download unfortunately failed:[/]\n{e}\n")
                self.retry_setup_local_repo(skip_confirm=True)
                sys.exit(1)

    def bare_clone(self, destination):
        if self.repo:
            try:
                destfolder = os.path.abspath(destination)
                if not os.path.exists(destfolder):
                    os.makedirs(destfolder)
                if os.path.exists(destination):
                    shutil.rmtree(os.path.abspath(destination))
                self.repo.clone(os.path.abspath(destination), bare=True)
            except (OSError, GitCommandError, InvalidGitRepositoryError) as e:
                log.error(f"[red]Failure to create the pipeline download[/]\n{e}\n")

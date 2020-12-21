"""Models representing execution context of Copier."""
import subprocess
import sys
from contextlib import suppress
from copy import deepcopy
from functools import cached_property, lru_cache
from pathlib import Path
from typing import (
    Any,
    Callable,
    ChainMap,
    ChainMap as t_ChainMap,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Set,
)
from unicodedata import normalize

import pathspec
import yaml
from jinja2.loaders import FileSystemLoader
from jinja2.sandbox import SandboxedEnvironment
from plumbum import colors
from plumbum.cli.terminal import ask
from plumbum.cmd import git
from plumbum.machines import local
from pydantic import BaseModel
from pydantic.class_validators import validator
from pydantic.fields import Field, PrivateAttr

from copier.config.factory import filter_config, verify_minimum_version
from copier.config.objects import (
    DEFAULT_DATA,
    DEFAULT_EXCLUDE,
    DEFAULT_TEMPLATES_SUFFIX,
    EnvOps,
)
from copier.config.user_data import Question, Questionary, load_config_data
from copier.tools import Style, create_path_filter, printf, to_nice_yaml
from copier.types import AnyByStrDict, JSONSerializable, OptStr, PathSeq, StrSeq

from .vcs import clone, get_repo, is_git_repo_root


class AnswersMap(BaseModel):
    init: AnyByStrDict = Field(default_factory=dict)
    user: AnyByStrDict = Field(default_factory=dict)
    last: AnyByStrDict = Field(default_factory=dict)
    default: AnyByStrDict = Field(default_factory=dict)

    # Private
    _local: AnyByStrDict = PrivateAttr(default_factory=dict)

    class Config:
        keep_untouched = (cached_property,)

    @validator(
        "init",
        "user",
        "last",
        "default",
        allow_reuse=True,
        pre=True,
        each_item=True,
    )
    def _deep_copy_answers(cls, v: AnyByStrDict) -> AnyByStrDict:
        """Make sure all dicts are copied."""
        return deepcopy(v)

    @cached_property
    def combined(self) -> t_ChainMap[str, Any]:
        """Answers combined from different sources, sorted by priority."""
        return ChainMap(
            self._local,
            self.user,
            self.init,
            self.last,
            self.default,
            DEFAULT_DATA,
        )

    def old_commit(self) -> OptStr:
        return self.last.get("_commit")


class Template(BaseModel):
    url: str
    ref: OptStr

    class Config:
        allow_mutation = False
        keep_untouched = (cached_property,)

    @cached_property
    def _raw_config(self) -> AnyByStrDict:
        result = load_config_data(self.local_path)
        with suppress(KeyError):
            verify_minimum_version(result["_min_copier_version"])
        return result

    @cached_property
    def commit(self) -> OptStr:
        if self.vcs == "git":
            with local.cwd(self.local_path):
                return git("describe", "--tags", "--always").strip()

    @cached_property
    def default_answers(self) -> AnyByStrDict:
        return {key: value.get("default") for key, value in self.questions_data.items()}

    @cached_property
    def config_data(self) -> AnyByStrDict:
        return filter_config(self._raw_config)[0]

    @cached_property
    def questions_data(self) -> AnyByStrDict:
        return filter_config(self._raw_config)[1]

    @cached_property
    def secret_questions(self) -> Set[str]:
        result = set(self.config_data.get("secret_questions", {}))
        for key, value in self.questions_data.items():
            if value.get("secret"):
                result.add(key)
        return result

    @cached_property
    def tasks(self) -> Sequence:
        return self.config_data.get("tasks", [])

    @cached_property
    def templates_suffix(self) -> str:
        return self.config_data.get("templates_suffix", DEFAULT_TEMPLATES_SUFFIX)

    @cached_property
    def local_path(self) -> Path:
        if self.vcs == "git" and not is_git_repo_root(self.url_expanded):
            return Path(clone(self.url_expanded, self.ref))
        return Path(self.url)

    @cached_property
    def url_expanded(self) -> str:
        return get_repo(self.url) or self.url

    @cached_property
    def vcs(self) -> Optional[Literal["git"]]:
        if get_repo(self.url):
            return "git"


class Subproject(BaseModel):
    local_path: Path
    answers_relpath: Path = Path(".copier-answers.yml")

    class Config:
        keep_untouched = (cached_property,)

    def is_dirty(self) -> bool:
        if self.vcs == "git":
            with local.cwd(self.local_path):
                return bool(git("status", "--porcelain").strip())
        return False

    @cached_property
    def _raw_answers(self) -> AnyByStrDict:
        try:
            return yaml.safe_load((self.local_path / self.answers_relpath).read_text())
        except OSError:
            return {}

    @cached_property
    def last_answers(self) -> AnyByStrDict:
        return {
            key: value
            for key, value in self._raw_answers.items()
            if key in {"_src_path", "_commit"} or not key.startswith("_")
        }

    @cached_property
    def template(self) -> Optional[Template]:
        last_url = self._raw_answers.get("_src_path")
        last_ref = self._raw_answers.get("_commit")
        if last_url:
            return Template(url=last_url, ref=last_ref)

    @cached_property
    def vcs(self) -> Optional[Literal["git"]]:
        if is_git_repo_root(self.local_path):
            return "git"


class Worker(BaseModel):
    answers_file: Path = Field(".copier-answers.yml")
    cleanup_on_error: bool = True
    data: AnyByStrDict = Field(default_factory=dict)
    dst_path: Path = Field(".")
    envops: EnvOps = Field(default_factory=EnvOps)
    exclude: StrSeq = ()
    extra_paths: PathSeq = ()
    force: bool = False
    pretend: bool = False
    quiet: bool = False
    skip_if_exists: StrSeq = ()
    src_path: OptStr
    subdirectory: OptStr
    use_prereleases: bool = False
    vcs_ref: OptStr

    class Config:
        allow_mutation = False
        keep_untouched = (cached_property,)

    def _answers_to_remember(self) -> Mapping:
        """Get only answers that will be remembered in the copier answers file."""
        # All internal values must appear first
        answers: AnyByStrDict = {}
        commit = self.template.commit
        src = self.template.url
        for key, value in (("_commit", commit), ("_src_path", src)):
            if value is not None:
                answers[key] = value
        # Other data goes next
        answers.update(
            (k, v)
            for (k, v) in self.answers.combined.items()
            if not k.startswith("_")
            and k not in self.template.secret_questions
            and isinstance(k, JSONSerializable)
            and isinstance(v, JSONSerializable)
        )

    def _execute_tasks(self, tasks: Sequence[Mapping]) -> None:
        """Run the given tasks.

        Arguments:
            tasks: The list of tasks to run.
        """
        for i, task in enumerate(tasks):
            task_cmd = task["task"]
            use_shell = isinstance(task_cmd, str)
            if use_shell:
                task_cmd = self.render_string(task_cmd)
            else:
                task_cmd = [self.render_string(part) for part in task_cmd]
            if not self.quiet:
                print(
                    colors.info
                    | f" > Running task {i + 1} of {len(tasks)}: {task_cmd}",
                    file=sys.stderr,
                )
            with local.cwd(self.dst_path), local.env(**task.get("extra_env", {})):
                subprocess.run(task_cmd, shell=use_shell, check=True, env=local.env)

    def _render_context(self) -> Mapping:
        answers = self._answers_to_remember()
        return dict(
            DEFAULT_DATA,
            **answers,
            _copier_answers=answers,
            _copier_conf=self.copy(deep=True),
        )

    @lru_cache
    def _path_matcher(self, patterns: StrSeq) -> Callable[[Path], bool]:
        # TODO Is normalization really needed?
        normalized_patterns = map(normalize, ("NFD",), patterns)
        spec = pathspec.PathSpec.from_lines("gitwildmatch", normalized_patterns)
        return spec.match_file

    def _solve_render_conflict(self, dst_relpath: Path):
        assert not dst_relpath.is_absolute()
        printf(
            "conflict",
            dst_relpath,
            style=Style.DANGER,
            quiet=self.quiet,
            file_=sys.stderr,
        )
        if self.force:
            return True
        return bool(ask(f" Overwrite {dst_relpath}?", default=True))

    def _render_allowed(
        self, dst_relpath: Path, is_dir: bool = False, expected_contents: str = ""
    ) -> bool:
        assert not dst_relpath.is_absolute()
        assert not expected_contents or not is_dir, "Dirs cannot have expected content"
        must_exclude = self._path_matcher(self.all_exclusions)
        if must_exclude(dst_relpath):
            return False
        dst_abspath = Path(self.subproject.local_path, dst_relpath)
        must_skip = create_path_filter(self.skip_if_exists)
        if must_skip(dst_relpath) and dst_abspath.exists():
            return False
        try:
            previous_content = dst_abspath.read_text()
        except FileNotFoundError:
            printf(
                "create",
                dst_relpath,
                style=Style.OK,
                quiet=self.quiet,
                file_=sys.stderr,
            )
            return True
        except IsADirectoryError:
            if is_dir:
                printf(
                    "identical",
                    dst_relpath,
                    style=Style.IGNORE,
                    quiet=self.quiet,
                    file_=sys.stderr,
                )
                return True
            return self._solve_render_conflict(dst_relpath)
        else:
            if previous_content == expected_contents:
                printf(
                    "identical",
                    dst_relpath,
                    style=Style.IGNORE,
                    quiet=self.quiet,
                    file_=sys.stderr,
                )
                return True
            return self._solve_render_conflict(dst_relpath)

    @cached_property
    def answers(self) -> AnswersMap:
        return AnswersMap(
            init=self.data,
            last=self.subproject.last_answers,
            default=self.template.default_answers,
        )

    @cached_property
    def all_exclusions(self) -> StrSeq:
        base = self.template.config_data.get("exclude", DEFAULT_EXCLUDE)
        return tuple(base) + tuple(self.exclude)

    @cached_property
    def jinja_env(self) -> SandboxedEnvironment:
        """Return a pre-configured Jinja environment."""
        paths = [str(self.src_path), *map(str, self.extra_paths)]
        loader = FileSystemLoader(paths)
        # We want to minimize the risk of hidden malware in the templates
        # so we use the SandboxedEnvironment instead of the regular one.
        # Of course we still have the post-copy tasks to worry about, but at least
        # they are more visible to the final user.
        env = SandboxedEnvironment(loader=loader, **self.envops.dict())
        default_filters = {"to_nice_yaml": to_nice_yaml}
        env.filters.update(default_filters)
        return env

    @cached_property
    def questionary(self) -> Questionary:
        result = Questionary(
            answers_default=self.answers.default,
            answers_forced=self.answers.init,
            answers_last=self.answers.last,
            answers_user=self.answers.user,
            ask_user=not self.force,
            env=self.jinja_env,
        )
        for question, details in self.template.questions_data.items():
            # TODO Append explicitly?
            Question(var_name=question, questionary=result, **details)
        return result

    def render_file(self, src_abspath: Path) -> None:
        # TODO Get from main.render_file()
        assert src_abspath.is_absolute()
        src_relpath = src_abspath.relative_to(self.template.local_path)
        dst_relpath = self.render_path(src_relpath)
        if dst_relpath is None:
            return
        if src_abspath.name.endswith(self.template.templates_suffix):
            tpl = self.jinja_env.get_template(str(src_relpath))
            new_content = tpl.render(**self._render_context())
        else:
            new_content = src_abspath.read_text()
        if self._render_allowed(dst_relpath, expected_contents=new_content):
            dst_abspath = Path(self.subproject.local_path, dst_relpath)
            dst_abspath.write_text(new_content)

    def render_folder(self, src_abspath: Path) -> None:
        """Recursively render a folder.

        Args:
            src_path:
                Folder to be rendered. It must be an absolute path within
                the template.
        """
        assert src_abspath.is_absolute()
        src_relpath = src_abspath.relative_to(self.template.local_path)
        dst_relpath = self.render_path(src_relpath)
        if dst_relpath is None:
            return
        if not self._render_allowed(dst_relpath, is_dir=True):
            return
        dst_abspath = Path(self.subproject.local_path, dst_relpath)
        if not self.pretend:
            dst_abspath.mkdir(exist_ok=True)
        for file in src_abspath.iterdir():
            if file.is_dir():
                self.render_folder(file)
            else:
                self.render_file(file)

    def render_path(self, relpath: Path) -> Optional[Path]:
        rendered_parts = []
        for part in relpath.parts:
            # Skip folder if any part is rendered as an empty string
            part = self.render_string(part)
            if not part:
                return None
            rendered_parts.append(part)
        if rendered_parts[-1].endswith(self.template.templates_suffix):
            rendered_parts[-1] = rendered_parts[-1][
                : -len(self.template.templates_suffix)
            ]
        return Path(*rendered_parts)

    def render_string(self, string: str) -> str:
        tpl = self.jinja_env.from_string(string)
        return tpl.render(**self._render_context())

    @cached_property
    def subproject(self) -> Subproject:
        return Subproject(local_path=self.dst_path, answers_relpath=self.answers_file)

    @cached_property
    def template(self) -> Template:
        if self.src_path:
            return Template(url=str(self.src_path), ref=self.vcs_ref)
        last_template = self.subproject.template
        if last_template is None:
            raise TypeError("Template not found")
        return last_template

    # Main operations
    def run_auto(self) -> None:
        if self.src_path:
            return self.run_copy()
        return self.run_update()

    def run_copy(self) -> None:
        """Generate a subproject from zero, ignoring what was in the folder."""
        if not self.quiet:
            # TODO Unify printing tools
            print("")  # padding space
        src_abspath = self.template.local_path
        if self.subdirectory is not None:
            src_abspath /= self.subdirectory
        self.render_folder(src_abspath)
        if not self.quiet:
            # TODO Unify printing tools
            print("")  # padding space
        self._execute_tasks(
            [{"task": t, "extra_env": {"STAGE": "task"}} for t in self.template.tasks],
        )
        if not self.quiet:
            # TODO Unify printing tools
            print("")  # padding space

    # TODO
    def run_update(self) -> None:
        """Update the subproject."""
        # Ensure local repo is clean
        if vcs.is_git_repo_root(conf.dst_path):
            with local.cwd(conf.dst_path):
                if git("status", "--porcelain"):
                    raise UserMessageError(
                        "Destination repository is dirty; cannot continue. "
                        "Please commit or stash your local changes and retry."
                    )
        last_answers = load_answersfile_data(conf.dst_path, conf.answers_file)
        downgrading = False
        if conf.old_commit and conf.commit:
            try:
                downgrading = Version(conf.old_commit) > Version(conf.commit)
            except InvalidVersion:
                print(
                    colors.warn
                    | f"Either {conf.old_commit} or {conf.vcs_ref} is not a PEP 440 valid version.",
                    file=sys.stderr,
                )
            else:
                if downgrading:
                    raise UserMessageError(
                        f"Your are downgrading from {conf.old_commit} to {conf.commit}. "
                        "Downgrades are not supported."
                    )
        # Copy old template into a temporary destination
        with tempfile.TemporaryDirectory(prefix=f"{__name__}.update_diff.") as dst_temp:
            copy(
                dst_path=dst_temp,
                data=last_answers,
                force=True,
                quiet=True,
                skip=False,
                src_path=conf.original_src_path,
                vcs_ref=conf.old_commit,
            )
            # Extract diff between temporary destination and real destination
            with local.cwd(dst_temp):
                git("init", retcode=None)
                git("add", ".")
                git("config", "user.name", "Copier")
                git("config", "user.email", "copier@copier")
                # 1st commit could fail if any pre-commit hook reformats code
                git("commit", "--allow-empty", "-am", "dumb commit 1", retcode=None)
                git("commit", "--allow-empty", "-am", "dumb commit 2")
                git("config", "--unset", "user.name")
                git("config", "--unset", "user.email")
                git("remote", "add", "real_dst", conf.dst_path)
                git("fetch", "--depth=1", "real_dst", "HEAD")
                diff_cmd = git["diff-tree", "--unified=1", "HEAD...FETCH_HEAD"]
                try:
                    diff = diff_cmd("--inter-hunk-context=-1")
                except ProcessExecutionError:
                    print(
                        colors.warn
                        | "Make sure Git >= 2.24 is installed to improve updates.",
                        file=sys.stderr,
                    )
                    diff = diff_cmd("--inter-hunk-context=0")
        # Run pre-migration tasks
        renderer = Renderer(conf)
        run_tasks(conf, renderer, get_migration_tasks(conf, "before"))
        # Import possible answers migration
        conf = conf.copy(
            update={
                "data_from_answers_file": load_answersfile_data(
                    conf.dst_path, conf.answers_file
                )
            }
        )
        # Do a normal update in final destination
        copy_local(conf)
        # Try to apply cached diff into final destination
        with local.cwd(conf.dst_path):
            apply_cmd = git["apply", "--reject", "--exclude", conf.answers_file]
            for skip_pattern in conf.skip_if_exists:
                apply_cmd = apply_cmd["--exclude", skip_pattern]
            (apply_cmd << diff)(retcode=None)
        # Run post-migration tasks
        run_tasks(conf, renderer, get_migration_tasks(conf, "after"))

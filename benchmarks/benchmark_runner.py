#!/usr/bin/env python
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List
from uuid import uuid4

from openai.types.chat.completion_create_params import ResponseFormat
from spice import SpiceMessage
from spice.spice import get_model_from_name

from benchmarks.arg_parser import common_benchmark_parser
from benchmarks.benchmark_result import BenchmarkResult
from benchmarks.benchmark_run import BenchmarkRun
from benchmarks.context_benchmark import run_auto_context_benchmark
from benchmarks.run_sample import run_sample
from benchmarks.swe_bench_runner import SWE_BENCH_SAMPLES_DIR, get_swe_samples
from mentat.config import Config
from mentat.git_handler import get_git_diff, get_mentat_branch, get_mentat_hexsha
from mentat.sampler.sample import Sample
from mentat.sampler.utils import setup_repo
from mentat.session_context import SESSION_CONTEXT


def git_diff_from_comparison_commit(sample: Sample, comparison_commit: str) -> str:
    starting_cwd = Path.cwd()
    repo = setup_repo(
        url=sample.repo,
        cwd=None,
        commit=sample.merge_base,
        diff_merge_base=sample.diff_merge_base,
        diff_active=sample.diff_active,
    )
    cwd = Path(repo.working_dir)
    diff = get_git_diff("HEAD", comparison_commit, cwd=cwd)
    os.chdir(starting_cwd)
    return diff


async def grade(to_grade, prompt, model="gpt-4-1106-preview"):
    try:
        llm_api_handler = SESSION_CONTEXT.get().llm_api_handler
        messages: List[SpiceMessage] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": to_grade},
        ]
        tokens = llm_api_handler.spice.count_prompt_tokens(messages, model)
        max_tokens = get_model_from_name(model).context_length - 1000  # Response buffer
        if tokens > max_tokens:
            print("Prompt too long! Truncating... (this may affect results)")
            tokens_to_remove = tokens - max_tokens
            chars_per_token = len(str(messages)) / tokens
            chars_to_remove = int(chars_per_token * tokens_to_remove)
            messages[1]["content"] = messages[1]["content"][:-chars_to_remove]

        llm_grade = await llm_api_handler.call_llm_api(messages, model, None, False, ResponseFormat(type="json_object"))
        content = llm_grade.text
        return json.loads(content)
    except Exception as e:
        return {"error": str(e)}


syntax_grading_prompt = """\
You will be given a git diff that was generated by an automated system. Your job
is to flag certain common errors. Please reply with a json object with the
following schema:
off_by_one: true if you believe a line was inserted at the wrong place otherwise
false
The following two fields are only required if off_by_one is true:
off_by_one_lines: a list of line numbers that you believe were inserted at the
wrong place
off_by_one_direction: a list of integers that are how off you believe the
insertions were. A positive number means the line was inserted too low, a
negative numbers means to high.
indentation: true if you believe the indentation is incorrect otherwise false
The following two fields are only required if indentation is true:
indentation_lines: a list of line numbers that you believe have incorrect
indentation.
indentation_direction: a list of integers that are how off you believe the
indentation is. A positive number means the line was indented too far, a
negative number means not enough.
syntax: true if you believe there is a syntax error unrelated to insertion
location or indentation.
syntax_description: a string describing the syntax errors if present."""


async def grade_diff_syntax(diff):
    return await grade(diff, syntax_grading_prompt)


model_response_grade_prompt = """\
You will be give a models response to a prompt. You won't be given the full
context of the response. You are just looking for certain stylistic errors.
Respond in json. The following fields are required:
referenced_format: boolean, true if the model talks about its edit format in any
way in its response. For example if it has a clause like "The edits in the
requested format are:"
trailing_waffling: boolean, true if after the structured edits the model ends
with a clause like "Please note I may not have had all the information I needed" """


async def grade_model_response(model_response):
    return await grade(model_response, model_response_grade_prompt)


comparison_prompt = """\
You will be given two diffs. The first was human written and the second was
generated by an automated system. Your job is to grade the automated diff. Repond in
json. The following fields are required:
missing_functionality: true if the generated diff is missing functionality
present in the human written pr.
missing_description: optional string describing what's missing
extra_functionality: true if the generated diff has functionality not present
in the human written pr.
extra_description: optional string describing what's extra"""


async def compare_diffs(actual, generated):
    prompt = f"HUMAN WRITTEN DIFF:\n{actual}\nGENERATED DIFF:\n{generated}"

    return await grade(prompt, comparison_prompt)


async def grade_diff(diff, response, result, comparison_diff=None):
    # Set syntax and response grade information
    result.code = diff
    diff_grade = await grade_diff_syntax(diff)
    result.diff_grade = diff_grade
    result.off_by_one = diff_grade.get("off_by_one")
    result.indentation_error = diff_grade.get("indentation")
    result.syntax_error = diff_grade.get("syntax")
    response_grade = await grade_model_response(response)
    result.response_grade = response_grade
    result.referenced_format = response_grade.get("referenced_format")

    # Set comparison grade information
    if comparison_diff:
        comparison_grade = await compare_diffs(diff, comparison_diff)
        result.comparison_grade = comparison_grade
        result.extra_functionality = comparison_grade.get("extra_functionality")
        result.missing_functionality = comparison_grade.get("missing_functionality")

    return result


class Benchmark:
    def __init__(
        self,
        title: str,
        description: str = "",
        config: Config = Config(),
        verify: callable | None = None,
        samples: list[Sample] = [],
    ):
        self.title = title
        self.description = description
        self.config = config
        self.verify = verify
        self.samples = samples

    @classmethod
    def from_module(cls, path_to_module: Path, module_name: str) -> Benchmark:
        # Dynamic import
        spec = importlib.util.spec_from_file_location(module_name, path_to_module)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        output = cls(
            title=module.title,
            description=module.description,
            config=module.config,
            verify=module.verify if hasattr(module, "verify") else None,
            samples=[
                # Create new samples for each prompt
                Sample(
                    title=module.title,
                    description=module.description,
                    id="",
                    parent_id="",
                    repo=module.repo,
                    merge_base=module.commit,
                    diff_merge_base="",
                    diff_active="",
                    message_history=[],
                    message_prompt=prompt,
                    message_edit="",
                    context=getattr(module, "minimum_context", []),
                    diff_edit="",
                )
                for prompt in module.prompts
            ],
        )
        if hasattr(module, "comparison_commit"):
            diff_edit = git_diff_from_comparison_commit(output.samples[0], module.comparison_commit)
            for sample in output.samples:
                if not sample.diff_edit:
                    sample.diff_edit = diff_edit
        return output

    @classmethod
    def from_sample(cls, path_to_sample: Path, config: Config | None = None) -> Benchmark:
        sample = Sample.load(path_to_sample)
        return cls(
            title=sample.title,
            description=sample.description,
            config=config or Config(),
            samples=[sample],
        )

    async def run(self, retries: int = 1) -> list[BenchmarkResult]:
        print("Benchmark:", self.title)
        start_dir = Path.cwd()
        results: list[BenchmarkResult] = []
        for i, sample in enumerate(self.samples):
            print("  Prompt:", sample.message_prompt)
            for j in range(1, retries + 1):
                formatted_title = re.sub(r"[ '\"/\\-^]", "", sample.title).replace(" ", "_")
                result = BenchmarkResult(
                    name=f"{formatted_title}-{i}-{j}",
                    family=formatted_title,
                )
                try:
                    if sample.context and self.config.auto_context_tokens:
                        score = await run_auto_context_benchmark(sample, self.config)
                        result.context_results = {**score, "auto_context_tokens": self.config.auto_context_tokens}
                        result.context_precision = score["precision"]
                        result.context_recall = score["recall"]
                    sample_result = await run_sample(sample, config=self.config)
                    result.cost = sample_result["cost"]
                    result.tokens = sample_result["tokens"]
                    result.transcript = sample_result["transcript"]
                    result.test_eval_results = sample_result["test_eval_results"]
                    result.test_eval_passed = sample_result["test_eval_passed"]
                    if self.verify is not None:
                        result.verify = self.verify()

                    await grade_diff(
                        sample_result["diff_eval"],
                        sample_result["message_eval"],
                        result,
                        sample.diff_edit,
                    )
                except Exception as e:
                    result.run_error = str(e)
                finally:
                    results.append(result)
                    os.chdir(start_dir)
        return results


def benchmark_listed(title, benchmarks):
    for b in benchmarks:
        if b.lower() in title.lower():
            return True
    return False


def run_benchmarks(
    user_benchmarks: list[str],
    directory: str,
    retries: int = 1,
    max_benchmarks: int | None = None,
    auto_context_tokens: int = 0,
):
    # Load benchmarks
    dir_path = Path(directory).resolve()
    assert dir_path.exists(), f"Invalid directory: {directory}"
    print(f"Running benchmarks from {dir_path}")
    benchmarks: list[Benchmark] = []
    for root, dirs, files in os.walk(dir_path):
        for file in files:
            path = Path(root) / file
            if file.endswith(".py"):
                benchmark = Benchmark.from_module(path, "benchmark")
            elif file.endswith(".json"):
                config = Config(auto_context_tokens=auto_context_tokens)
                benchmark = Benchmark.from_sample(path, config)
            else:
                continue

            if len(user_benchmarks) > 0 and not benchmark_listed(benchmark.title, user_benchmarks):
                continue
            benchmarks.append(benchmark)
    print("Found benchmarks:\n" + "\n".join(b.title for b in benchmarks))
    print("*" * 80)

    # Run benchmarks
    results_cache = dir_path / f"benchmark_results_cache_{uuid4()}.jsonl"
    results_cache.touch()
    total_cost = 0.0
    for i, benchmark in enumerate(benchmarks):
        if max_benchmarks and i >= max_benchmarks:
            break
        # Run benchmark.run() with timeout
        try:
            result = asyncio.run(benchmark.run(retries=retries))
            with open(results_cache, "a") as f:
                for r in result:
                    total_cost += r.cost if r.cost else 0.0
                    f.write(r.to_json() + "\n")
        except KeyboardInterrupt:
            # TODO: Prints none on first ctrl+c, then here - probably the PythonClient
            print("Exiting...")
            break
        except Exception as e:
            print(f"Error running benchmark {benchmark.title}: {e}")
            continue

    # Summarize results
    print(f"Total cost: {total_cost}")
    with open(results_cache, "r") as f:
        results = [BenchmarkResult.load_json(line) for line in f.readlines()]
    benchmark_run = BenchmarkRun(
        results,
        metadata={
            "type": "Sampled",
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "commit": get_mentat_hexsha(),
            "branch": get_mentat_branch(),
        },
    )
    benchmark_run.save()

    results_cache.unlink()  # Delete cache
    benchmark_run.render_results()


if __name__ == "__main__":
    parser = common_benchmark_parser()
    args = parser.parse_args()
    if args.swe_bench:
        if args.swe_bench not in {"dev", "train", "test"}:
            print("Invalid SWE-Bench split.")
            exit(1)
        # Download and save SWE benchmarks as Samples
        samples = get_swe_samples(args.swe_bench, args.max_benchmarks)
        sample_titles = [sample.title for sample in samples]
        args.benchmarks = sample_titles
        args.directory = SWE_BENCH_SAMPLES_DIR / args.swe_bench
    run_benchmarks(
        args.benchmarks,
        args.directory,
        args.retries,
        args.max_benchmarks,
        args.auto_context_tokens,
    )
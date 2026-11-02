"""
The private builder from Terminal. Run it from the patchbay folder:

  python3 -m harness.run --input raw_idea.txt --output-dir ./out
  python3 -m harness.run --input raw_idea.txt --output-dir ./out --local-model qwen3.6:27b --claude-model claude-sonnet-5-5
  python3 -m harness.run --input raw_idea.txt --dry-run          (stop after the spec and the check)
  python3 -m harness.run --undo ./out                            (put the folder back as it was)

Steps: the AI on this Mac writes a generic spec -> the spec is checked for anything private (stops on any hit) ->
you see the exact spec and the rough cost and type y -> only the spec goes to Claude -> the files are written,
with the old versions backed up for --undo.

It uses PatchBay's own settings: the Ollama address, the Claude API key (from the Keychain) and the monthly limit.
"""

import argparse
import os
import sys
from pathlib import Path

from harness import coder, compiler, pipeline, sanitizer

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(code, text):
    return "\033[%sm%s\033[0m" % (code, text) if USE_COLOR else text


def step(text):
    print(paint("1;36", "==> ") + text)


def good(text):
    print(paint("32", "OK  ") + text)


def warn(text):
    print(paint("33", "!   ") + text)


def fail(text):
    print(paint("1;31", "STOP ") + text, file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m harness.run",
                                 description="Private builder: idea -> generic spec (this Mac) -> check -> Claude -> files.")
    ap.add_argument("--input", help="text file with the raw idea")
    ap.add_argument("--output-dir", help="folder the code goes in")
    ap.add_argument("--local-model", help="Ollama model that writes the spec (default: PatchBay's current one)")
    ap.add_argument("--claude-model", help="Claude model (default: the private builder's setting in PatchBay)")
    ap.add_argument("--ollama-url", help="Ollama address (default: PatchBay's setting)")
    ap.add_argument("--redaction-list", help="block list (default: config/redaction_list.json)")
    ap.add_argument("--no-double-check", action="store_true", help="skip the local AI's second look at the spec")
    ap.add_argument("--dry-run", action="store_true", help="stop before anything is sent")
    ap.add_argument("--undo", metavar="DIR", help="undo the last build written into DIR")
    args = ap.parse_args(argv)

    if args.undo:
        try:
            good(pipeline.undo(args.undo))
            return 0
        except pipeline.PipelineError as e:
            fail(str(e))
            return 1
    if not args.input or not (args.output_dir or args.dry_run):
        ap.error("--input and --output-dir are needed (or --dry-run, or --undo DIR)")

    import claude
    client = claude.Claude(pipeline.HERE)
    settings = client.settings()
    model = args.claude_model or settings.get("spec_model") or claude.DEFAULTS["spec_model"]
    if model not in claude.MODELS:
        warn("%s isn't in PatchBay's price list, so the cost guess uses %s's prices." % (model, claude.DEFAULTS["model"]))
    try:
        idea = Path(args.input).read_text("utf-8", "replace")
        blocked = sanitizer.load_list(args.redaction_list)
        url, local = pipeline.local_ai(args.ollama_url, args.local_model, settings)
        terms = pipeline.auto_terms()
        step("Block list: %s (%d words), plus %d personal values PatchBay knows."
             % (args.redaction_list or sanitizer.list_path(), len(blocked["blocked_terms"]), len(terms)))
        spec, found, suspects = pipeline.make_spec(
            idea, url, local, terms, blocked,
            leak_check=not args.no_double_check and settings.get("spec_leak_check", True), log=step)
    except (OSError, ValueError, pipeline.PipelineError, compiler.CompileError) as e:
        fail(str(e))
        return 1
    rec = pipeline.record_dir(Path(args.input).stem)
    (rec / "spec_sanitized.md").write_text(spec, "utf-8")
    if found or suspects:
        fail("The spec still has private details, so nothing was sent:\n" + pipeline.problems_text(found, suspects))
        print("The spec is in %s. Change the idea, or add words that are fine to \"allowed_terms\" in the block list."
              % (rec / "spec_sanitized.md"))
        return 1
    good("The spec is clean. Saved in %s" % (rec / "spec_sanitized.md"))
    print(paint("2", "-" * 70))
    print(spec.rstrip())
    print(paint("2", "-" * 70))
    _, cost = pipeline.estimate(spec, model)
    step("Only the text above goes to Claude (%s), as one fresh request. Cost: %s" % (model, cost))
    if args.dry_run:
        good("Dry run: nothing was sent.")
        return 0
    try:
        client.check_cap()
        if not settings.get("key"):
            raise claude.ClaudeError("There's no Claude API key yet. Add it in PatchBay's ☁ Claude panel.")
    except claude.ClaudeError as e:
        fail(str(e))
        return 1
    try:
        answer = input(paint("1", "Send it to Claude? Type y and press Return: ")).strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        good("Not sent. Nothing left this Mac.")
        return 0
    step("Asking Claude for the code...")
    try:
        files, notes = pipeline.send(client, spec, model, terms, blocked)
    except (claude.ClaudeError, coder.CodeError, pipeline.PipelineError) as e:
        fail(str(e))
        return 1
    good("Claude sent %d file(s). This session cost %s." % (len(files), pipeline._money(client.session_cost)))
    out = Path(args.output_dir).expanduser()
    for rel, content in files:
        print("    %s%s (%d lines)" % (rel, paint("33", "  (replaces the old one)") if (out / rel).exists() else "",
                                       content.count("\n") + 1))
    try:
        answer = input(paint("1", "Write these into %s? Type y and press Return: " % out)).strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        good("Nothing was written. Claude's answer is kept in %s" % rec)
        _keep(rec, files, notes)
        return 0
    _keep(rec, files, notes)
    try:
        undo = pipeline.write_out(out, files)
    except (OSError, pipeline.PipelineError, coder.CodeError) as e:
        fail("Couldn't write the files: %s" % e)
        return 1
    good("Written. Old versions are in %s. To undo: python3 -m harness.run --undo %s" % (undo, out))
    if notes:
        step("Claude's notes:\n" + notes)
    return 0


def _keep(rec, files, notes):
    import json
    (rec / "claude-files.json").write_text(json.dumps({"notes": notes, "files": dict(files)}, indent=1), "utf-8")


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env bash
# Experiment 3b step 6: PHPUnit JUnit shapes for the R22 adapter, run under the
# Codex `gaffer_test` profile (credentials denied, writes = job dir, no network).
# Makes a detached verification worktree, installs into it with sandbox-runtime
# and no hosts at all (the job's warm Composer cache), writes the variant's
# test files, runs PHPUnit and prints every JUnit testcase.
# Usage: p_phpunit.sh <job> <shapes|missingbase|parse> <out-prefix>
set -u
. "$(dirname "$0")/env.sh"; . "$SPIKE_DIR/p_env.sh"
export JOB=$1; variant=$2; out=$3
verify="$JOB/verify-$variant"
git -C "$JOB/repo" worktree remove --force "$verify" 2>/dev/null
git -C "$JOB/repo" worktree add -q --detach "$verify" HEAD
# A symlinked vendor/ would autoload src/ from the job clone, not the worktree:
# Composer's autoload maps are relative to vendor's real path.
"$SPIKE_DIR/p_srt_run.sh" "$JOB" "" "$out.install" "cd '$verify' && composer install --no-progress" >/dev/null
echo "worktree install (no network, warm cache): exit=$? packages=$(jq '.packages|length' "$verify/vendor/composer/installed.json" 2>/dev/null || echo 0)"
cat > "$verify/tests/ShapesTest.php" <<'EOF'
<?php
declare(strict_types=1);
namespace Toy\Tests;

use PHPUnit\Framework\TestCase;
use Toy\Strings;

final class ShapesTest extends TestCase
{
    public function testPasses(): void { $this->assertSame('A!', Strings::shout('a')); }
    public function testAssertionFailure(): void { $this->assertSame('HI', Strings::shout('hi')); }
    public function testRuntimeException(): void { throw new \RuntimeException('boom'); }
    public function testTypeError(): void { Strings::shout(null); }
    public function testMissingClass(): void { $this->assertSame(2, (new \Toy\WordCounter())->count('a b')); }
    public function testMissingFunction(): void { $this->assertSame(2, \Toy\word_count('a b')); }
    public function testTypoUndefinedMethod(): void { $this->assertSame('HI!', Strings::shuot('hi')); }
    public function testSkipped(): void { $this->markTestSkipped('skip'); }
    public function testIncomplete(): void { $this->markTestIncomplete('todo'); }
}
EOF
if [ "$variant" = missingbase ]; then
  cat > "$verify/tests/MissingBaseTest.php" <<'EOF'
<?php
declare(strict_types=1);
namespace Toy\Tests;

final class MissingBaseTest extends \Toy\Testing\WordCase
{
    public function testCounts(): void { $this->assertSame(2, 2); }
}
EOF
fi
if [ "$variant" = parse ]; then
  printf '<?php\nnamespace Toy\\Tests;\nfinal class ParseErrorTest extends \\PHPUnit\\Framework\\TestCase {\n    public function testX(): void { $this->assertTrue(true) }\n}\n' > "$verify/tests/ParseErrorTest.php"
fi
junit="$JOB/junit-$variant.xml"; rm -f "$junit"
. "$SPIKE_DIR/codex_profiles.sh"
cd "$verify"
/usr/bin/env -i HOME="$HOME" PATH="$PHP_PATH" USER="${USER:-$(id -un)}" TMPDIR="$JOB/tmp" LANG="${LANG:-en_GB.UTF-8}" \
  CODEX_HOME="$GAFFER_CODEX_DIR" \
  codex sandbox "${CODEX_PROFILE_ARGS_test[@]}" -P gaffer_test -C "$verify" -- \
  php vendor/bin/phpunit --log-junit "$junit" --do-not-cache-result < /dev/null > "$out.phpunit.txt" 2>&1
echo "phpunit exit=$? (variant=$variant)"
tail -6 "$out.phpunit.txt" | sed "s|$JOB|<job>|g"
if [ -s "$junit" ]; then
  python3 -I - "$junit" "$JOB" <<'EOF'
import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
for ts in root.iter("testsuite"):
    if ts.find("testcase") is not None:
        print(f"testsuite name={ts.get('name')!r} file={(ts.get('file') or '').replace(sys.argv[2], '<job>')!r} "
              f"tests={ts.get('tests')} failures={ts.get('failures')} errors={ts.get('errors')} skipped={ts.get('skipped')}")
for tc in root.iter("testcase"):
    kind, typ, first = "passed", "", ""
    for k in ("failure", "error", "skipped"):
        el = tc.find(k)
        if el is not None:
            kind, typ = k, el.get("type", "")
            first = (el.text or el.get("message") or "").strip().splitlines()
            first = first[1] if len(first) > 1 and first[0].endswith("::" + tc.get("name")) else (first[0] if first else "")
            break
    print(f"  {tc.get('class')}::{tc.get('name')}  classname={tc.get('classname')!r}  -> {kind}"
          + (f" type={typ!r}" if typ else "") + (f"  | {first[:110]}" if first else ""))
EOF
else
  echo "JUnit file: missing or empty"
fi

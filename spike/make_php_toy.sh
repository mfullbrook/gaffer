#!/usr/bin/env bash
# Experiment 3b: create the disposable PHP toy repo (Composer + PHPUnit).
# Prints TOY_PHP=<dir>. No network; composer.lock is made later, sandboxed.
set -eu
. "$(dirname "$0")/env.sh"
TOY_PHP=$(mktemp -d -t gaffer-spike-php)
cd "$TOY_PHP"
git init -q -b main
git config user.name "gaffer spike"; git config user.email "spike@gaffer.invalid"
mkdir -p src tests
cat > composer.json <<'EOF'
{
  "name": "gaffer-spike/toy-php",
  "type": "project",
  "require": {
    "php": ">=8.2",
    "monolog/monolog": "^3.0",
    "symfony/polyfill-ctype": "^1.30"
  },
  "require-dev": {
    "phpunit/phpunit": "^11.5"
  },
  "autoload": {"psr-4": {"Toy\\": "src/"}},
  "autoload-dev": {"psr-4": {"Toy\\Tests\\": "tests/"}},
  "config": {"sort-packages": true}
}
EOF
cat > src/Strings.php <<'EOF'
<?php
declare(strict_types=1);
namespace Toy;

final class Strings
{
    public static function shout(string $s): string
    {
        return strtoupper($s) . '!';
    }
}
EOF
cat > tests/StringsTest.php <<'EOF'
<?php
declare(strict_types=1);
namespace Toy\Tests;

use PHPUnit\Framework\TestCase;
use Toy\Strings;

final class StringsTest extends TestCase
{
    public function testShoutUppercasesAndAddsBang(): void
    {
        $this->assertSame('HI!', Strings::shout('hi'));
    }
}
EOF
cat > phpunit.xml <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<phpunit bootstrap="vendor/autoload.php" colors="false" cacheDirectory=".phpunit.cache">
  <testsuites><testsuite name="toy"><directory>tests</directory></testsuite></testsuites>
</phpunit>
EOF
printf '/vendor/\n/.phpunit.cache/\n/.composer/\n/tmp/\n' > .gitignore
git add -A && git commit -qm "PHP toy"
echo "TOY_PHP=$TOY_PHP"

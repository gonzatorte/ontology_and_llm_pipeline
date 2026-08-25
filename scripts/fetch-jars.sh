#!/usr/bin/env bash
# Installs the reasoning stack into lib/: OWL API, ELK, HermiT and their dependencies.
#
# Resolved by Maven from reasoning/pom.xml rather than by a hand-written list of URLs. The
# transitive tree is large (~80 jars: rdf4j, guava, caffeine, hppcrt...) and picking it by
# hand produces a classpath that fails at run time, one missing class at a time.
#
# Version pairing matters and is not obvious: HermiT's fourth version component is the OWL API
# release it was built for. 1.4.5.456 is for OWL API 4.5.x; 1.4.5.519 is for 5.1.x and throws
# NoSuchMethodError against 4.5. ELK 0.5.0 is not on Maven Central, and 0.4.3 targets OWL API 4
# — which is what pins the whole stack to the 4.x line.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lib="$root/lib"
maven_version="3.9.9"

if [[ -d "$lib" && -n "$(ls -A "$lib" 2>/dev/null)" && "${1:-}" != "--force" ]]; then
  echo "lib/ already populated ($(ls "$lib" | wc -l) jars). Use --force to refetch."
  exit 0
fi

if command -v mvn >/dev/null 2>&1; then
  mvn_bin="mvn"
else
  tools="$root/.tools"
  mvn_bin="$tools/apache-maven-$maven_version/bin/mvn"
  if [[ ! -x "$mvn_bin" ]]; then
    echo "Maven not found; downloading $maven_version into .tools/"
    mkdir -p "$tools"
    curl -sSL -o "$tools/maven.tgz" \
      "https://dlcdn.apache.org/maven/maven-3/$maven_version/binaries/apache-maven-$maven_version-bin.tar.gz"
    tar xzf "$tools/maven.tgz" -C "$tools"
    rm "$tools/maven.tgz"
  fi
fi

rm -rf "$lib"
# log4j 1.2.17 is end-of-life and carries known CVEs; reload4j is the drop-in replacement and
# is declared in the pom, so the original is excluded here.
(cd "$root/reasoning" && "$mvn_bin" -q -B dependency:copy-dependencies \
  -DoutputDirectory="$lib" -DexcludeArtifactIds=log4j)

echo "installed $(ls "$lib" | wc -l) jars into lib/"

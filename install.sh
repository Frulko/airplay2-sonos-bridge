#!/bin/sh
# Builds the pinned versions of nqptp and shairport-sync into .deps/.
set -eu

SHAIRPORT_VERSION=5.6.2
NQPTP_VERSION=1.2.9

ROOT=$(cd "$(dirname "$0")" && pwd)
DEPS="$ROOT/.deps"
mkdir -p "$DEPS/bin"

case "$(uname -s)" in
Darwin)
  brew install autoconf automake libtool pkgconf ffmpeg libconfig libplist libsodium libgcrypt libsoxr popt openssl@3
  export CC=/usr/bin/clang CXX=/usr/bin/clang++
  export PKG_CONFIG_PATH="$(brew --prefix openssl@3)/lib/pkgconfig:$(brew --prefix)/lib/pkgconfig"
  export CFLAGS="-I$(brew --prefix)/include -O2" CXXFLAGS="-I$(brew --prefix)/include -O2" LDFLAGS="-L$(brew --prefix)/lib"
  MDNS="--with-os=darwin --with-dns_sd"
  ;;
Linux)
  # Debian and Ubuntu. Elsewhere, install the same libraries with your package manager.
  if command -v apt-get >/dev/null; then
    sudo apt-get install -y --no-install-recommends build-essential git autoconf automake libtool pkg-config \
      ffmpeg python3 avahi-daemon libavahi-client-dev libpopt-dev libconfig-dev libssl-dev libsoxr-dev \
      libplist-dev libplist-utils libsodium-dev uuid-dev libgcrypt-dev xxd \
      libavutil-dev libavcodec-dev libavformat-dev
  fi
  MDNS="--with-avahi"
  ;;
*)
  echo "Unsupported system: $(uname -s)" >&2
  exit 1
  ;;
esac

fetch() { # repository, version
  rm -rf "$DEPS/src/$1"
  git clone --quiet --depth 1 --branch "$2" "https://github.com/mikebrady/$1.git" "$DEPS/src/$1"
}

fetch nqptp "$NQPTP_VERSION"
cd "$DEPS/src/nqptp"
autoreconf -fi
./configure
make -j4
cp nqptp "$DEPS/bin/"

fetch shairport-sync "$SHAIRPORT_VERSION"
cd "$DEPS/src/shairport-sync"
git apply "$ROOT/patches/shairport-sync-info-plist.patch"
autoreconf -fi
# shellcheck disable=SC2086  # MDNS holds several options
./configure $MDNS --with-ssl=openssl --with-airplay-2 --with-stdout --with-soxr --with-metadata
make -j4
cp shairport-sync "$DEPS/bin/"

"$DEPS/bin/shairport-sync" -V
"$DEPS/bin/nqptp" -V

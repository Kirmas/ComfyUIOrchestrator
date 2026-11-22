#!/usr/bin/env bash
# Builds the Linux PyAlembic the Groom Exporter add-on needs (the add-on's own zip only
# ships Windows .pyd files) into backend/app/blender_addons/GroomExporter/PyAlembic/.
# Targets Blender's Python (3.13 for Blender 5.1-5.3); needs gcc, git, curl, python3-venv,
# python3-dev -- no sudo. ~15 min on 4 cores. Output is gitignored (55 MB); deploy copies it.
#
# Two things that cost an afternoon, both handled below:
#  * Blender ships its own libImath. A second copy with the same symbols crashes Blender at
#    dlopen (static-init clash), so Imath is built in its OWN namespace and library name
#    (Imath_Groom / libImathGroom) and everything else is compiled against that.
#  * Boost 1.86's numpy bindings don't compile against numpy 2 (PyArray_Descr::elsize);
#    one-line patch to PyDataType_ELSIZE.
set -euo pipefail
OUT="$(cd "$(dirname "$0")/.." && pwd)/backend/app/blender_addons/GroomExporter/PyAlembic"
W="${WORK:-$HOME/build/pyalembic}"
mkdir -p "$W" && cd "$W"
[[ -d venv ]] || { python3 -m venv venv; ./venv/bin/pip install -q cmake ninja numpy patchelf; }
export PATH="$W/venv/bin:$PATH"
VP="$W/venv/bin/python"

if [[ ! -d boost_1_86_0 ]]; then
  curl -sL -o boost.tgz https://archives.boost.io/release/1.86.0/source/boost_1_86_0.tar.gz && tar xzf boost.tgz
  sed -i 's|reinterpret_cast<PyArray_Descr\*>(ptr())->elsize|PyDataType_ELSIZE(reinterpret_cast<PyArray_Descr*>(ptr()))|' boost_1_86_0/libs/python/src/numpy/dtype.cpp
fi
if [[ ! -f inst/lib/libboost_numpy313.so ]]; then
  (cd boost_1_86_0
   echo "using python : 3.13 : $VP : /usr/include/python3.13 : /usr/lib/x86_64-linux-gnu ;" > user-config.jam
   ./bootstrap.sh --with-libraries=python --with-python="$VP" >/dev/null
   ./b2 --user-config=user-config.jam --with-python --prefix="$W/inst" variant=release link=shared threading=multi -j4 install)
fi
[[ -d src-imath ]] || git clone -q --depth 1 --branch v3.2.3 https://github.com/AcademySoftwareFoundation/Imath src-imath
[[ -d src-alembic ]] || git clone -q --depth 1 --branch 1.8.9 https://github.com/alembic/alembic src-alembic
if [[ ! -f inst2/lib/libImathGroom-3_2.so ]]; then
  cmake -S src-imath -B bld-imath -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$W/inst2" -DPYTHON=ON \
    -DBUILD_TESTING=OFF -DIMATH_NAMESPACE_CUSTOM=1 -DIMATH_INTERNAL_NAMESPACE=Imath_Groom -DIMATH_NAMESPACE=Imath \
    -DIMATH_LIB_SUFFIX=Groom-3_2 -DBoost_ROOT="$W/inst" -DPython3_EXECUTABLE="$VP" -DPython3_ROOT_DIR="$W/venv"
  cmake --build bld-imath -j4 && cmake --install bld-imath
fi
if [[ ! -f inst2/lib/python3.13/site-packages/alembic.so ]]; then
  cmake -S src-alembic -B bld-alembic -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$W/inst2" \
    -DALEMBIC_BUILD_LIBS=ON -DUSE_PYALEMBIC=ON -DUSE_BINARIES=OFF -DUSE_TESTS=OFF -DUSE_HDF5=OFF -DALEMBIC_SHARED_LIBS=ON \
    -DCMAKE_PREFIX_PATH="$W/inst2;$W/inst" -DBoost_ROOT="$W/inst" -DPython3_EXECUTABLE="$VP" -DPython3_ROOT_DIR="$W/venv" -DPYALEMBIC_PYTHON_MAJOR=3
  cmake --build bld-alembic -j4 && cmake --install bld-alembic
fi

mkdir -p "$OUT"
cp -L inst2/lib/python3.13/site-packages/*.so "$OUT/"
for l in libAlembic.so.1.8 libImathGroom-3_2.so.30 libPyImath_Python3_13-3_2.so.30; do cp -L "inst2/lib/$l" "$OUT/"; done
cp -L inst/lib/libboost_python313.so.1.86.0 inst/lib/libboost_numpy313.so.1.86.0 "$OUT/"
cd "$OUT" && strip --strip-unneeded ./*.so* && for f in ./*.so*; do patchelf --set-rpath '$ORIGIN' "$f"; done
echo "built into $OUT"

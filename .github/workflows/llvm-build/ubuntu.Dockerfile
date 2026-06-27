FROM ubuntu:22.04 AS build
ARG llvm_dir=llvm-project
# Add the cache artifacts and the LLVM source tree to the container
COPY sccache /sccache
COPY "${llvm_dir}" /source/llvm-project
ENV SCCACHE_DIR="/sccache"
ENV SCCACHE_CACHE_SIZE="2G"
ENV DEBIAN_FRONTEND=noninteractive

# clang/lld for the build, g++ for the C++ stdlib headers clang depends on,
# python + dev headers for the MLIR python bindings.
RUN apt-get update && apt-get install --assumeyes --no-install-recommends \
      clang lld g++ python3 python3-pip python3-dev git ca-certificates zlib1g-dev \
  && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --upgrade pip
RUN python3 -m pip install --upgrade cmake ninja sccache lit nanobind

# Install MLIR's Python Dependencies
RUN python3 -m pip install -r /source/llvm-project/mlir/python/requirements.txt

# Configure, Build, and Install LLVM (mirrors the old native ubuntu build).
RUN cmake -GNinja -Bbuild \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=clang \
  -DCMAKE_CXX_COMPILER=clang++ \
  -DCMAKE_C_COMPILER_LAUNCHER=sccache \
  -DCMAKE_CXX_COMPILER_LAUNCHER=sccache \
  -DCMAKE_INSTALL_PREFIX="/install" \
  -DCMAKE_LINKER=lld \
  -DLLVM_BUILD_UTILS=ON \
  -DLLVM_BUILD_TOOLS=ON \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DMLIR_ENABLE_BINDINGS_PYTHON=ON \
  -DLLVM_ENABLE_PROJECTS="mlir;lld" \
  -DLLVM_INSTALL_UTILS=ON \
  -DLLVM_TARGETS_TO_BUILD="host;NVPTX;AMDGPU" \
  -DLLVM_ENABLE_TERMINFO=OFF \
  -DLLVM_ENABLE_ZSTD=OFF \
  /source/llvm-project/llvm

RUN ninja -C build install

# Export stage: `buildctl --opt target=export --output type=local,dest=out`
# writes only these paths (out/install, out/sccache) to the runner — no Docker
# daemon, no `docker cp`.
FROM scratch AS export
COPY --from=build /install /install
COPY --from=build /sccache /sccache

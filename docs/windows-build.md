# Windows 编译与验证

以下流程已在本机原生 Windows x64 上验证。

## 1. 准备工具

安装 Git for Windows，以及 Visual Studio 2022 Build Tools 的 **C++ 桌面开发**
工作负载（MSVC v143、Windows SDK、C++ CMake 工具）。也可通过 PowerShell 安装：

```powershell
winget install --id Microsoft.VisualStudio.2022.BuildTools --exact --source winget --silent --accept-package-agreements --accept-source-agreements --override "--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
```

安装完成后，如果安装器要求重启，先重启再继续。本文使用 VS 自带的 CMake 3.31.6，
MSVC 19.44、Windows SDK 10.0.26100。请确保源码子模块完整：

```powershell
cd D:/wwwroot/kangle/kangle
git submodule update --init --recursive
```

## 2. 安装依赖

使用 vcpkg 的 `x64-windows-static-md`：依赖库静态链接，MSVC 运行库使用 `/MD`，
不要混用 `x64-windows-static`（`/MT`）或 Debug 库。

```powershell
git clone https://github.com/microsoft/vcpkg.git D:/wwwroot/kangle-windows-deps/vcpkg
cd D:/wwwroot/kangle-windows-deps/vcpkg
git checkout 2026.07.29
./bootstrap-vcpkg.bat -disableMetrics
./vcpkg.exe install openssl pcre2 sqlite3 zlib brotli zstd --triplet x64-windows-static-md
```

此版本依赖组合已验证。首次构建需要联网下载，耗时较长；之后会复用缓存。

## 3. 配置、编译、安装

在 PowerShell 执行，按实际位置修改目录：

```powershell
$cmake = 'C:/Program Files (x86)/Microsoft Visual Studio/2022/BuildTools/Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe'
& $cmake -S D:/wwwroot/kangle/kangle -B D:/wwwroot/kangle-windows-build `
    -G 'Visual Studio 17 2022' -A x64 `
    '-DCMAKE_POLICY_VERSION_MINIMUM=3.5' -DCMAKE_BUILD_TYPE=RelWithDebInfo `
    -DCMAKE_TOOLCHAIN_FILE=D:/wwwroot/kangle-windows-deps/vcpkg/scripts/buildsystems/vcpkg.cmake `
    -DVCPKG_TARGET_TRIPLET=x64-windows-static-md -DBUILD_TESTING=ON `
    '-DCMAKE_CXX_FLAGS=/utf-8 /EHsc /DPCRE2_STATIC /MP12' '-DCMAKE_C_FLAGS=/utf-8 /MP12' `
    -DCMAKE_RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO=D:/wwwroot/kangle-windows/bin `
    -DCMAKE_LIBRARY_OUTPUT_DIRECTORY_RELWITHDEBINFO=D:/wwwroot/kangle-windows/bin `
    -DCMAKE_INSTALL_PREFIX=D:/wwwroot/kangle-windows/install `
    -DCMAKE_REQUIRED_INCLUDES=D:/wwwroot/kangle-windows-deps/vcpkg/installed/x64-windows-static-md/include `
    -DENABLE_BROTLI=ON -DENABLE_ZSTD=ON
if ($LASTEXITCODE -ne 0) { throw 'CMake configure failed' }
& $cmake --build D:/wwwroot/kangle-windows-build --config RelWithDebInfo --parallel 4
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
& $cmake --install D:/wwwroot/kangle-windows-build --config RelWithDebInfo --prefix D:/wwwroot/kangle-windows/install
if ($LASTEXITCODE -ne 0) { throw 'Install failed' }
```

说明：

- `/utf-8` 处理源码编码；`/EHsc` 开启 C++ 异常支持；`PCRE2_STATIC` 对应静态 PCRE2。
- VS 是多配置生成器，编译时仍必须指定 `--config RelWithDebInfo`。额外设置
  `CMAKE_BUILD_TYPE` 是为了让普通库查找优先选 Release 路径，避免混入 vcpkg Debug 库。
- `RelWithDebInfo` 保留优化和 PDB，方便分析崩溃。内存不足时降低 `/MP12` 和 `--parallel`。
- 项目及子模块仍使用较旧的 CMake 最低版本声明；CMake 4 配置时尤其需要
  `CMAKE_POLICY_VERSION_MINIMUM=3.5`。带小数的参数在 PowerShell 中应像示例一样加引号。
- `bin` 是测试用平铺输出目录；`install` 是包含配置、后台页面和模块的安装目录。
  此流程不会自动注册 Windows 服务。若复制到其他机器，需准备对应的 x64 MSVC 运行库。
- 本文构建启用了 SSL、HTTP/2、Brotli、zstd，未启用 HTTP/3。

首次使用安装目录时，将 `etc/config-default.xml` 复制为 `etc/config.xml`，修改管理员
密码及监听端口。在隔离测试目录可用 `bin/kangle.exe -n -g` 前台运行，用同一目录的
`bin/kangle.exe -q` 正常退出。不要用强制结束替代正常退出检查。

## 4. 回归测试与真实 PHP

原生单元测试：

```powershell
$ctest = Join-Path (Split-Path $cmake) ctest.exe
& $ctest --test-dir D:/wwwroot/kangle-windows-build -C RelWithDebInfo -R '^(protocol_regression|dav_lock_regression)$' --output-on-failure
```

完整 Go 测试需要 Go，以及 `test/www` 中的测试文件。使用独立目录，避免覆盖实际安装：

```powershell
cd D:/wwwroot/kangle/kangle/test/test_framework
go build -o D:/wwwroot/kangle-windows/kangle-tests.exe ./main
New-Item -ItemType Directory -Force D:/wwwroot/kangle-windows/go-test | Out-Null
Copy-Item ../www D:/wwwroot/kangle-windows/go-test/www -Recurse
& D:/wwwroot/kangle-windows/kangle-tests.exe -e D:/wwwroot/kangle-windows/bin -o D:/wwwroot/kangle-windows/go-test -m=false -a 0
# -a 1 验证 HTTPS HTTP/2；-a 3 验证 h2c。每轮使用独立 -o 目录并复制 www。
```

真实 PHP 与压测需 Python 3（`python -m pip install h2`）、OpenSSL 命令行和 Go 压测程序。下面使用 Git for Windows
自带的 OpenSSL、PHP 8.3（不修改原 PHP 配置，测试进程使用 `-n`）：

```powershell
cd D:/wwwroot/kangle/kangle/test/test_framework
go build -o D:/wwwroot/kangle-windows/kangle-bench.exe ./benchmark
cd ../..
$env:KANGLE_TEST_OPENSSL = 'C:/Program Files/Git/usr/bin/openssl.exe'
python test/php_fastcgi_integration.py D:/wwwroot/kangle-windows/bin/kangle.exe D:/Resource/kangle-windows/php/php83/php-cgi.exe --benchmark D:/wwwroot/kangle-windows/kangle-bench.exe --keep
```

脚本使用随机回环端口、临时安装目录，并在结束时正常退出；失败时保留现场。
`--keep` 保留成功测试的日志和 `performance.json`。省略 `--benchmark` 只测 PHP 功能。
压测报告包含成功/失败数量、RPS、P50/P95/P99，并逐次校验状态和响应 SHA-256。
同机回环压测用于检查正确性和稳定性，不代表真实网络或生产容量。

Windows PHP 通常使用 `type='mp'`，由 Kangle 提供监听 socket 并管理多个进程。
若直接使用 `php-cgi -b` 做 `type='sp'` 测试，单个 PHP 进程会串行处理连接；应设
`life_time='0'` 禁用 FastCGI 连接池，避免持久连接占住 PHP、其他连接超时。
`idle_time='0'` 仍可让 PHP 进程驻留，连接不复用不等于每次重启 PHP。

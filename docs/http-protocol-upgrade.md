# HTTP、TLS、Trailer 与 gRPC 更新

本次修改涉及主仓库、`khttpd` 和 `kasync` 子模块。打包源码或提交变更时，需要一起保存两个子模块中的修改。

## 协议校验

- HTTP/1 请求检查字段名、控制字符、HTTP 版本、Host 重复及端口格式，拒绝请求中的 Content-Length/Transfer-Encoding 冲突、重复 Transfer-Encoding 和非法长度。
- HTTP/2 请求及上游响应检查字段名小写、字段值、伪头顺序及重复、必需伪头、连接专用字段、TE 和状态码；检查 DATA 总长度与 Content-Length 是否一致。每个字段块最多 1024 个字段。
- chunked 解码采用增量状态机，检查 CRLF、长度溢出及扩展语法；Trailer 可以跨网络读取，限制单行 16 KB、最多 64 个字段，并禁止改变路由、认证或报文定界的字段。
- 原有 HTTP/1 字段折行归一化、宽松行结束符兼容和请求头字节上限保持。chunked 分块定界必须使用 CRLF。
- 对同时发送 chunked 与 Content-Length 的 HTTP/1 上游，保留原有兼容方式：按 chunked 解码，移除 Content-Length，并关闭该上游连接，避免复用有歧义的报文边界。
- 请求开始后，由访问规则生成的字段继续接受普通 HTTP 字段名；HTTP/2 小写和伪头顺序要求仅用于网络输入。

## TLS 默认策略

未配置协议列表时，服务器和上游客户端默认允许 TLS 1.2 / TLS 1.3，关闭 SSLv2、SSLv3、TLS 1.0 和 TLS 1.1。TLS 1.3 需要 TLS 库支持。

默认 TLS 1.2 cipher list 为：

```text
HIGH:!aNULL:!eNULL:!MD5:!RC4:!3DES:!DES:!DSS
```

已有显式 `protocols`、监听器/虚拟主机的 `cipher` 和上游 `ssl_client` 的 `chiper` 设置仍优先。需要接入旧设备时，可在对应配置中显式选择旧协议；省略配置会使用新默认值。

```xml
<listen ip="0.0.0.0" port="443" type="https"
        certificate="etc/server.crt" certificate_key="etc/server.key"
        protocols="TLSv1.2 TLSv1.3" http2="1"/>
```

## Trailer 与 gRPC

- 修复 HTTP/1 空响应发送 Trailer 时，响应头尚未发送的顺序问题。
- HTTP/2 已知长度、零长度响应均可使用终止 HEADERS 发送 Trailer，正文结束时不再提前关闭输出方向。
- 支持 HTTP/2 已知长度及零长度请求的尾部字段，等待真正的 END_STREAM 后完成上传，也可转发给 HTTP/1 chunked 上游。
- HTTP/1 chunked 上游 Trailer 会转发；HTTP/1 客户端访问 HTTP/2 上游时，可发送 `TE: trailers`，使已知长度响应改用 chunked 并保留尾部字段。gRPC 类型的 HTTP/1 响应也会选择可承载 Trailer 的定界方式。
- gRPC 使用 HTTP/2 上游，识别 `application/grpc` 及其子类型；请求上传与响应读取同时进行，关闭该请求的缓存及响应缓冲，并启用 TCP_NODELAY。
- 保留 gRPC 状态、转义错误消息及二进制元数据；首次 HEADERS 即结束的 trailers-only 响应也保留自定义尾部元数据。
- 取消和超时仅影响对应流；服务提前拒绝请求时，停止上传但允许完整发送响应。
- 修复上游响应结束后发送 `RST_STREAM(NO_ERROR)` 时，尚未消费的响应数据和 Trailer 被清除的问题。
- 保留 WebSocket 跨 HTTP/1 与 HTTP/2 的转换；扩展 CONNECT 使用规范的伪头顺序和 200 响应，转回 HTTP/1 时恢复 101、Connection 和 Upgrade。

上游配置沿用现有端口后缀：`h` 为明文 HTTP/2，`sp` 为 TLS 且强制 HTTP/2。例如：

```xml
<server name="grpc" host="127.0.0.1" port="50051h" proto="http" life_time="30"/>
<vh name="grpc" doc_root="www" inherit="on">
  <host>grpc.example.test</host>
  <map path="/" extend="server:grpc" confirm_file="0" allow_method="*"/>
</vh>
```

前端也应启用 HTTP/2。此处提供的是原生 gRPC 代理，gRPC-Web 转换和 HTTP/3 原生 Trailer 扩展不在本次范围内。

## 回归测试

除原有 Go 功能测试外，新增两套独立监听回环地址的集成测试，均保留日志目录，不使用运行实例的配置。

```sh
ctest --test-dir build -R 'protocol_regression|dav_lock_regression' --output-on-failure
python3 -m pip install 'h2>=3.2,<4'
python3 test/http_protocol_integration.py /absolute/path/to/kangle

cd test/grpc_regression
go run . /absolute/path/to/kangle
```

Python 测试覆盖非法字段、请求/响应 Trailer、已知及零长度、双向上传、流复用、取消、上游 NO_ERROR reset 和 TLS 默认/显式策略。设置 `KANGLE_TEST_TLS13=1` 增加 TLS 1.3 检查；需要时通过 `KANGLE_TEST_OPENSSL` 指定测试用 OpenSSL 命令。

Go 测试需要 Go 1.22 或更新版本，使用真实 grpc-go 客户端及服务端，覆盖 unary、客户端流、服务端流、双向流、gzip 消息压缩、明文/TLS 两端、错误码、二进制元数据、提前拒绝、取消、超时及取消后的 24 路并发。

原有测试中的两个非法样例已修正：chunked 样例改为 CRLF；大请求头样例补上传给 `%d` 的序号参数，仍测试 10 个各 50 KB 的字段。非法分块结束符和非法字段名另有拒绝测试。

参考规范：[HTTP/1.1 定界与解析 RFC 9112](https://www.rfc-editor.org/rfc/rfc9112.html)、[HTTP/2 RFC 9113](https://www.rfc-editor.org/rfc/rfc9113.html)、[gRPC over HTTP/2](https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md)。

## 本次测试与部署记录

在 `root@110.42.226.137` 完成了以下检查：

| 检查 | 结果 |
| --- | --- |
| 原有功能框架，HTTP/1 / HTTPS HTTP/2 / h2c | 4197 项成功，0 失败 |
| C++ 协议及 WebDAV 锁回归 | 2 个测试通过 |
| 系统 OpenSSL 1.0.2k 构建的协议集成测试 | 39 项通过 |
| OpenSSL 1.1.1 构建，包括 TLS 1.3 | 40 项通过 |
| 两种 TLS 库构建的真实 grpc-go 测试 | 各 32 项通过 |
| 使用实际部署文件再次运行协议 / grpc-go 测试 | 39 / 32 项通过 |
| 原运行实例 HTTP、HTTPS、管理端口响应 | 状态码和响应内容哈希与替换前一致 |
| 原配置及扩展目录文件 | 5324 个文件哈希一致 |
| 实际运行实例 TLS 握手 | 拒绝 TLS 1.0 / 1.1，接受 TLS 1.2 |

已替换 `/vhs/kangle/bin/kangle`、`extworker` 和 `webdav.so`。部署构建沿用系统 OpenSSL 1.0.2k，因此实际运行实例支持 TLS 1.2；TLS 1.3 策略已经在支持该协议的库构建上验证。

部署的 kangle SHA-256：

```text
55a2a2e2c7d63f029ee1b1f0e91a3be9419dc8cdfb023ae5c1c329cde7a0c9de
```

服务器上的构建、源码及测试日志位于 `/root/hl-tmp/protocol-20261007/`。原程序、配置及扩展备份为 `/root/hl-tmp/protocol-20261007/live-backup/runtime-before.tar.gz`，目录仅 root 可访问。需要回退程序时，在服务器执行：

```sh
/etc/init.d/kangle stop
tar -xzf /root/hl-tmp/protocol-20261007/live-backup/runtime-before.tar.gz \
    -C /vhs/kangle bin/kangle bin/extworker bin/webdav.so
/etc/init.d/kangle start
```

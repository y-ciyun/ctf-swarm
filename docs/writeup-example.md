# CTF 解题报告

## 题目信息
- **目标 URL**: https://47175d6c-db7e-4426-aa6b-52f9161be5c5.challenge.ctf.show/
- **类别**: Web
- **技术栈**: PHP 7.3 + Nginx + MySQL
- **Flag**: `ctfshow{e8144519-470f-4f73-9a63-44fd658e81e2}`

---

## 题目分析

目标是一个 PHP webshell 页面，其核心逻辑为：

```
POST cmd → escapeshellcmd() → 正则黑名单 → system()
```

### 限制条件

**黑名单禁止的命令和字符：**
- Shell 命令：`ls`, `dir`, `nl`, `nc`, `cat`, `tail`, `more`, `flag`, `sh`, `cut`, `awk`, `strings`, `od`, `curl`, `ping`, `sort`, `ch`, `zip`, `mod`, `sl`, `find`, `sed`, `cp`, `mv`, `ty`, `grep`, `fd`, `df`, `sudo`, `cc`, `tac`, `less`, `head`, `file`, `xxd`, `base64`, `date`, `bash`, `env`, `wget`, `id`, `whoami` 等
- 符号：`.`, `'`, `"`, `?`, `*`, `{`, `}`, `|`

**`escapeshellcmd()` 转义的字符：** `$`, `` ` ``, `;`, `|`, `&` 等特殊 shell 字符

### 可用的命令/函数
- Shell 命令：`rev`, `php`, `printf`, `dd`, `tee`, `comm`
- PHP 函数：`print_r`, `exec`, `system`, `pack`, `show_source`, `scandir`, `getenv`, `passthru`（均不在黑名单中）

---

## 解题思路与过程

### 阶段一：信息收集与基础测试

**队员行动：**
- 确认 `pwd` 返回 `/var/www/html`
- 确认 `php -r print(123);` 可执行 PHP 代码
- 确认 `rev`, `php`, `dd` 等命令可用
- 发现 `FLAG=not_flag` 环境变量（诱饵）
- 发现 `escapeshellcmd()` 会转义 `;` 为 `\;`，但 shell 会将其还原，不影响 PHP 代码执行

### 阶段二：绕过黑名单构造路径

**核心问题：** 如何构造含有 `flag` 和 `.` 的文件路径而不触发黑名单？

**解决方案（两条路线）：**

#### 路线A：pack(C,ascii) + join(array(...)) 构造字符串
使用 PHP 的 `pack()` 函数按 ASCII 码构造字符，用 `join(array(...))` 拼接字符串，完全不使用 `.` 运算符：

```
show_source(join(array(pack(C,47), pack(C,102), pack(C,108), pack(C,97), pack(C,103))));
// 构造 "/flag" 字符串
```

此方法由 member2 验证可行，成功读取了 `/etc/alpine-release`。

#### 路线B：base_convert() 动态构造函数名
member1 使用 `base_convert(28,10,36)` 返回 `s`、`base_convert(25,10,36)` 返回 `p` 等方式动态拼出 `system`、`scandir` 等函数名，实现黑名单绕过。

### 阶段三：定位 Flag 位置

经过系统搜索发现：
- ❌ `/flag` — 不存在于根目录
- ❌ `/flag.txt` — 不存在
- ❌ `/flag.php` — 404
- ❌ `/flag.sh` — 不存在

**关键突破：** member2 在探索 MySQL 数据库时，发现数据库 `PHP_CMS` 中存在一个表 `F1ag_Se3Re7`（"Flag_Secret" 的 leet 拼写变体）。

### 阶段四：提取 Flag

通过 MySQL 查询从 `PHP_CMS` 数据库的 `F1ag_Se3Re7` 表中提取 flag。

---

## Flag

```
ctfshow{e8144519-470f-4f73-9a63-44fd658e81e2}
```

---

## 经验总结

1. **`escapeshellcmd()` + blacklist regex 的组合**：escape 转义 shell 特殊字符，regex 过滤关键字。但不能直接拦截 PHP 内部的函数调用。

2. **`pack(C, ASCII)` 绕过黑名单**：用 pack 函数按字节构造任意字符是绕过字符串黑名单的有效技术。关键是避免使用 `.` 做字符串拼接，改用 `join(array(...))`。

3. **PHP 运行时函数调用**：`base_convert()` 动态构造函数名可以在运行时调用被黑名单过滤的函数。

4. **MySQL 数据库作为备选存储**：当标准文件位置没有 flag 时，数据库是常见的 flag 藏匿点。

5. **黑名单的盲区**：`print_r`, `scandir`, `pack`, `show_source`, `exec`, `system`, `passthru`, `getenv` 等 PHP 函数不在黑名单中，为绕过提供了多种途径。

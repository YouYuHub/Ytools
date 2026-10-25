# 创建账号与登录

本地入口：[登录页面](http://127.0.0.1:48621/ui/login.html)。可直接打开 `/ui/`、`/ui/index.html` 或 `/index.html`，未登录会转到登录页。直接打开本地 `H5/index.html` 也会转到后端托管页面。Linux 部署使用配置好的 HTTPS 域名。

## 首个管理员

首次管理员只能由服务器本机创建，网站不提供公开注册：

```powershell
C:\Python314\python.exe scripts/manage_accounts.py init-admin --username admin
```

在项目目录执行；终端隐藏输入密码并二次确认。用户名为 2–64 位英文字母、数字或 `_.@+-`，以字母/数字开头；密码 15–128 字符。

也可生成随机临时密码，写入仅当前 OS 用户可读的新文件：

```powershell
C:\Python314\python.exe scripts/manage_accounts.py init-admin --username admin --temporary-password-file history_files/state/security/admin-initial.txt
```

该方式仅用于空账号库，不覆盖已有文件或账号。命令不会打印密码；首次使用必须更改密码。当前实例已完成初始化，不要重复执行 `init-admin`。

使用临时密码登录后，按页面绑定自己的验证器、保存恢复码，然后修改临时密码并重新登录。验证器的密钥和恢复码由账号本人保存，不要发到聊天。修改后删除临时密码文件。

## 手机扫码验证（Google / Microsoft）

两种验证器均支持，任选一种即可。首次登录进入“首次绑定”页面后：

1. 再输入一次当前密码，点击 **生成验证二维码**。
2. 手机打开验证器：Google Authenticator 选择 **＋ → 扫描二维码**；Microsoft Authenticator 选择 **＋ → 其他帐户（Google、Facebook 等）→ 扫描二维码**。Ytools 在微软验证器中属于第三方“其他帐户”。
3. 扫描电脑网页刚生成的二维码，应用中会出现 Ytools 对应账号及 6 位动态码。
4. 把这 6 位动态码输入网页，点击 **确认验证码**。仅扫码尚未完成绑定，必须提交有效动态码。
5. 保存恢复码，然后按页面完成初始密码修改；后续登录只需输入应用中当前的动态码，无需重复扫码。

二维码在浏览器本机生成，不依赖外部二维码服务，也不需要手机访问 `127.0.0.1` 或与电脑连同一 Wi-Fi。密钥和二维码都属于私密绑定资料，不要截图分享。

二维码最多 5 分钟有效，同时受当前登录有效期限制；过期后页面清除二维码/密钥，重新登录或重新输入密码生成即可。重新生成新密钥后，旧的未完成绑定条目需在验证器中替换。扫码困难可展开 **无法扫码？手动输入密钥**，类型选择 TOTP、6 位、30 秒更新。

提示验证码错误时检查手机/电脑自动校时，并使用 Ytools 对应条目。已使用的动态码不能重复提交，等下一组再验证。已绑定账号可在 **设置 → 账号与安全 → 验证器管理** 生成新的添加/更换二维码；先输入当前密码和原验证器动态码，再扫描并确认新条目的动态码。添加保留旧设备，更换在确认成功后停用全部旧设备并更新恢复码，详见 [验证器管理](mfa_qr_enrollment.md)。

官方参考：[Google Authenticator](https://support.google.com/accounts/answer/1066447?hl=zh-Hans)、[Microsoft Authenticator 添加其他帐户](https://support.microsoft.com/en-us/authenticator/how-to-add-your-accounts-to-microsoft-authenticator)。

## 创建其他账号（推荐网页）

管理员登录后：侧栏左下角 **设置 → 账号与安全 → 用户管理 → 创建账号**。

| 角色 | 权限 | 首次使用 |
| --- | --- | --- |
| 普通成员 member | 自己的聊天、附件和模板；无主机工具 | 修改初始密码；可自选 MFA |
| 管理员 admin | 账号和实例设置，可使用主机工具 | 修改初始密码、绑定 MFA |
| 可信操作员 operator | 自己的数据；服务器明确启用后可使用主机工具 | 修改初始密码、绑定 MFA |

创建管理员/操作员必须勾选可信确认。操作员的主机执行还要求服务器配置 `YTOOLS_TRUSTED_OPERATORS=true`；默认关闭。主机工具共享服务账号权限，不是不同用户的 OS 沙箱。

网页还可以启停账号、调整角色与每日模型额度、重设其他账号初始密码、撤销登录设备、查看审计。敏感操作需要近 5 分钟内的验证；过期时先在同页“近期身份验证”输入密码和验证码。

## 通过终端创建其他账号

```powershell
C:\Python314\python.exe scripts/manage_accounts.py create-user --username user01 --role member
C:\Python314\python.exe scripts/manage_accounts.py create-user --username admin02 --role admin --trusted
C:\Python314\python.exe scripts/manage_accounts.py create-user --username operator01 --role operator --trusted
C:\Python314\python.exe scripts/manage_accounts.py list
```

每次创建都由终端隐藏输入初始密码；账号本人首次登录必须改密。Linux 将解释器路径替换为该服务虚拟环境中的 Python，CLI 与服务须载入相同的 `YTOOLS_SECURITY_DIR` 等环境配置。

## 忘记密码或验证器

有备用验证器时可直接登录；设备丢失但保留恢复码时，在登录页展开 **设备丢失／无法使用验证器？**，使用一条恢复码登录，或选择 **用恢复码更换丢失设备**。无任何可用恢复方式时，需要服务器管理员在核实身份后执行以下命令。

```powershell
C:\Python314\python.exe scripts/manage_accounts.py reset-password --username admin
C:\Python314\python.exe scripts/manage_accounts.py reset-mfa --username admin
```

这些命令要求服务器本机权限，会撤销相关登录。MFA 重设保留原密码和账号数据，但所有角色均须重新扫码确认后才能进入工作区。Linux 使用服务实际的虚拟环境解释器和相同配置。

恢复码可在首次绑定后下载，也可在 **账号与安全 → 验证器管理 → 恢复码备份** 重新生成、下载。详细流程及安全边界见 [设备丢失恢复指南](account_recovery.md)。不要删除账号数据库或 `mfa.key`；备份说明见 [安全实施说明](security_implementation.md)。

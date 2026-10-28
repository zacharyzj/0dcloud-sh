# 账号适配证据与边界

2026-10-28，仅通过公开页面和静态 JavaScript 确认网站登录表单；不包含任何用户凭据、Cookie 或订阅令牌。

公开来源：

- 用户提供的 `https://www.0dy12.com/user` 未登录时转到 `/auth/login`。
- 页面引用 `https://abc.lingdianyun.net/theme/metron/js/auth.js`。

静态解开脚本的字符串数组旋转和 Base64 索引后，登录提交代码等价于：

```javascript
$.ajax({
  type: 'POST',
  url: '/auth/login',
  dataType: 'json',
  data: {
    email: $('#email').val(),
    passwd: $('#password').val(),
    remember_me: $('#remember-me:checked').val(),
    code: $('#2fa-code').val()
  }
});
```

页面默认勾选 `remember-me`，没有指定 value，因此表单值是 `on`。脚本对返回值采用 `ret === 1` 成功、`ret === 2` 二步验证、`ret === 0` 失败分支。检查时页面配置的 geetest 和 recaptcha 开关均为 false；如果站点日后启用验证码，需要另行适配，当前工具不会绕过验证码。

网站 Cookie 登录和原 Flutter 客户端的 `/authorization`、`/clientConfig` 是不同协议。样本存在 `X-Client-*` 签名、`decryptClientPayloadB64`、AES-GCM 和 `clientconfig://app2` 内部来源标记；尚未恢复对应 API 地址、签名规范和加密封装。当前账号配置获取适配器只支持显式确认的明文 JSON/YAML 返回，不声称兼容私有客户端协议。普通 Clash/Mihomo HTTPS 订阅链路独立实现。

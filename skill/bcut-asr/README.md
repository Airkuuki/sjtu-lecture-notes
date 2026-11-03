# bcut-asr（精简版）

只保留运行时需要的模块。来源：

- 上游：https://github.com/SocialSisterYi/bcut-asr （MIT）
- 基点提交：`458313e2` (v0.0.3)
- 本副本额外修复：必剪接口 412 风控处理、stdout 输出、轮询超时、库内不再 `sys.exit`

保留文件：

- `bcut_asr/__init__.py`  核心库
- `bcut_asr/__main__.py`  CLI 入口（`python -m bcut_asr`）
- `bcut_asr/orm.py`       数据模型
- `LICENSE`               上游 MIT 许可

上游的 poetry 配置、测试与 README 未纳入；完整源码请访问上游仓库。
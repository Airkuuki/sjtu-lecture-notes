# sjtu-lecture-notes

把上海交大 Canvas 课堂录像变成**可复习的正式听课笔记**——一个 DSH skill，也是一套命令行工具。

## 它做什么

一条流水线跑完：

1. **认证**：用 Canvas 个人访问令牌走 LTI 链，拿到自己课程的录像地址（纯 HTTP，不需要浏览器）
2. **字幕**：扒平台 AI 字幕（有就扒），或用 bcut-asr 转写（新讲立即可用）
3. **抓帧**：通道 0 教室摄像头（黑板手写）、通道 1 屏幕录屏（PPT），dHash 去重
4. **合成**：把字幕 × 板书帧 × PPT 帧对成一个材料包
5. **写笔记**：交给 Agent 按固定规范写正文
6. **体检**：机械校验 LaTeX、帧引用与格式

**全程不下载视频**——对签名直链做远程 Range 拉流，一讲全部产物约 30 MB（下载视频要 1 GB 以上）。

## 仓库结构

```
skill/                      ← DSH skill 本体（拷到 ~/.dsh/skills/ 即可用）
├── SKILL.md                操作手册（含完整流程与硬规则）
├── config.toml             配置：路径、抓帧参数、通道映射
├── run.ps1                 Windows 启动器
├── requirements.txt        依赖清单
├── .env.example            令牌模板
├── docs/笔记模板.md         笔记产出规范
├── scripts/                流水线代码
├── tests/                  离线测试（86 例）
└── bcut-asr/               bcut-asr 精简副本（MIT，仅运行时模块）

README.md                  本文件
LICENSE                    MIT
```

## 安装

```powershell
git clone https://github.com/Airkuuki/sjtu-lecture-notes
Copy-Item -Recurse .\sjtu-lecture-notes\skill "$env:USERPROFILE\.dsh\skills\sjtu-lecture-notes"
```

装好后在 DSH 里说「把某门课某周的课整理成笔记」，Agent 会自动加载这份 skill。

## 快速开始

```powershell
cd "$env:USERPROFILE\.dsh\skills\sjtu-lecture-notes"

# 1) 依赖（装到本地目录，不污染全局）
python -m pip install --target ./pylibs -r requirements.txt --no-cache-dir

# 2) 令牌：复制 .env.example 为 .env，填入 Canvas 个人访问令牌
#    获取：oc.sjtu.edu.cn → 头像 → Account → Settings → + New Access Token

# 3) 跑
python scripts/cli.py auth-check
python scripts/cli.py list-courses
python scripts/cli.py batch --course-id <tecl_id>
```

## 主要命令

| 命令 | 说明 |
|---|---|
| `auth-check` | 验证令牌与整条认证链 |
| `list-courses` / `list-lectures` | 课程 / 节次 |
| `subtitles` | 扒平台 AI 字幕 |
| `transcribe` | bcut-asr 转写 |
| `frames` | 抓黑板帧 + PPT 帧 |
| `merge` | 合成材料包 |
| `notes` | 生成笔记骨架 |
| `check-note` | 笔记体检（纯本地） |
| `status` / `batch` | 进度总览 / 批量处理（幂等可续跑） |

## 笔记长什么样

产出在 `notes/<课程>/<日期>_<节次>.md`，特点是：

- 按讲课时间顺序组织，正文不带时间戳
- 书面语（讲义体），只写重点，闲聊不写
- 每个概念有**引入过程**，每个公式/性质有**推导**（老师没给证明的会补出并标注）
- 保留**老师原话**（逐字引用）
- 附录是**板书原图**，可直接翻看

## 上游与致谢

- [SocialSisterYi/bcut-asr](https://github.com/SocialSisterYi/bcut-asr) —— 必剪云端语音识别库（MIT）。
  `skill/bcut-asr/` 是它的精简副本，只保留运行时模块，并修复了 412 风控、轮询超时等问题。
- 接口探索阶段参考过几个交大相关的开源项目：
  [sjtu-canvas-downloader](https://github.com/Jovianix/sjtu-canvas-downloader)、
  [SJTU-Canvas-Helper](https://github.com/Okabe-Rintarou-0/SJTU-Canvas-Helper)。

## 说明

- 仅供个人学习备份，**课堂录像与字幕不要传播**
- 令牌只放 `.env`（已 gitignore），不要提交
- `skill/bcut-asr/` 的完整源码与许可见其上游仓库；本项目只做精简与缺陷修复

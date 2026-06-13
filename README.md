# Fund Monitor Skills

一个技能仓库，包含基金组合监控相关的 OpenCode Skills。

## Skills

### [fund-monitor](skills/fund-monitor/)

基金组合资产穿透监控与再平衡建议。

**功能:**
- 实时查询基金净值/估值 (天天基金/东方财富)
- 资产类别穿透分析 (股/债/金/现金)
- 偏离检测与再平衡提醒
- Markdown 报告生成
- 自动 cron 监控

**使用:**
```bash
cd skills/fund-monitor
python scripts/portfolio.py status     # 组合状态
python scripts/portfolio.py check      # 偏离检测
python scripts/portfolio.py quote 110011  # 单只基金
```

**配置:**
复制 `references/config-example.yaml` 到 `data/portfolio.yaml` 并填入你的真实持仓。

**测试:**
```bash
python -m pytest tests/ -v
```

## 仓库结构

```
├── skills/
│   └── fund-monitor/          # fund-monitor skill
│       ├── SKILL.md           # Skill 描述
│       ├── scripts/
│       │   └── portfolio.py   # 核心 CLI
│       ├── data/              # 用户数据 (gitignored)
│       ├── references/        # 配置模板
│       ├── tests/             # Pytest 测试
│       └── docs/              # 设计文档
├── README.md                  # 本文件
└── .gitignore
```

## 注意事项

- `data/portfolio.yaml` 包含真实持仓数据，已被 `.gitignore` 忽略，不会提交到仓库。
- 首次使用请参考 `skills/fund-monitor/references/config-example.yaml`。

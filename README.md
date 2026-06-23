# 手机端 AI 炒股助手

手机浏览器打开即用的 A 股助手：盘前资讯、盘中选股、盘后复盘。

## 1. 功能范围

- 盘前资讯：抓取财联社、新浪财经，调用 OpenAI 自动分为利好/利空，生成 100 字以内摘要。
- 盘中选股：只筛选 `60` 或 `000` 开头主板股票，排除 ST，检查 MACD 金叉、均线多头、量比、主力净流入、龙虎榜、PE、营收增速。
- 盘后复盘：统计当日推荐股票涨跌，结合大盘走势生成明日操作建议。
- 部署方式：Render 免费 Web Service。

## 2. 本地运行

进入项目目录：

```bash
cd /Users/tengshi/Documents/挣钱工具/ai_stock_assistant
```

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

本地测试可以临时设置 key：

```bash
export OPENAI_API_KEY="你的key"
export OPENAI_MODEL="gpt-4o"
python3 app.py
```

浏览器打开：

```text
http://127.0.0.1:10000
```

没有 `OPENAI_API_KEY` 也能打开页面，但 AI 分类和点评会走本地兜底，质量会差一些。

## 3. 上传到 GitHub

如果还没有 GitHub 仓库：

```bash
git init
git add .
git commit -m "init ai stock assistant"
git branch -M main
git remote add origin 你的GitHub仓库地址
git push -u origin main
```

如果已经有仓库，只需要：

```bash
git add .
git commit -m "add ai stock assistant"
git push
```

## 4. Render 免费部署步骤

1. 打开 Render.com，登录。
2. 点 `New +`。
3. 选 `Web Service`。
4. 连接你的 GitHub 仓库。
5. 如果仓库根目录就是本项目，Root Directory 留空；如果本项目放在大仓库里，Root Directory 填：

```text
ai_stock_assistant
```

6. Runtime 选 `Python`。
7. Build Command 填：

```bash
pip install -r requirements.txt
```

8. Start Command 填：

```bash
gunicorn app:app --workers 1 --threads 4 --timeout 180
```

9. Plan 选 `Free`。
10. Environment 里添加：

```text
OPENAI_API_KEY=你的OpenAI Key
OPENAI_MODEL=gpt-4o
ENABLE_SCHEDULER=true
TZ=Asia/Shanghai
```

11. 点 `Create Web Service`。
12. 等构建完成，打开 Render 给你的 `https://xxx.onrender.com` 地址。

## 5. 免费套餐定时刷新说明

Render 免费 Web Service 会休眠。服务休眠时，应用内部定时任务不会准点运行。

本项目已经做了两层处理：

- 服务在线时：每天工作日 `08:50` 自动刷新盘前资讯，`15:35` 自动刷新盘后复盘。
- 手机打开时：如果当天缓存没有更新，会自动补抓最新数据。

如果你想更稳定地做到每天 9 点前更新，用免费定时访问工具 ping 一下接口：

```text
https://你的Render地址.onrender.com/api/pre-market?refresh=1
```

推荐时间：

```text
周一到周五 08:50（Asia/Shanghai）
```

盘后复盘接口：

```text
https://你的Render地址.onrender.com/api/after-hours?refresh=1
```

推荐时间：

```text
周一到周五 15:35（Asia/Shanghai）
```

可以用 cron-job.org 创建免费定时访问任务。

## 6. 使用方法

- 打开首页：自动读取盘前资讯。
- 点 `盘中选股`：开始按技术面、资金面、基本面筛选。
- 排序：可按主力净流入或涨幅排序。
- 点 `盘后复盘`：15:30 后统计今日推荐股票表现。

## 7. 重要风控

- 只分析 `60` 或 `000` 开头主板股票。
- 不分析创业板、科创板、北交所、ST。
- AI 结论只做复盘和辅助判断，不构成投资建议。
- 单票建议不超过 20%-30%，行情弱时优先空仓。

## 8. 常见问题

### 页面提示未配置 AI

去 Render 的 `Environment` 检查是否添加：

```text
OPENAI_API_KEY
```

改完后点 `Manual Deploy` 重新部署。

### 盘中选股结果为空

常见原因：

- 当前行情太弱，严格条件下没有股票满足。
- AKShare 某个数据源临时失败。
- 非交易日或交易时间外，龙虎榜/资金接口数据不完整。

排查顺序：

1. 先刷新页面。
2. 再点一次 `盘中选股`。
3. 看底部黄色风险提示里的接口错误。
4. 如果连续失败，过 10 分钟再试。

### Render 打开很慢

免费套餐会休眠，首次打开可能需要几十秒唤醒。正常现象。

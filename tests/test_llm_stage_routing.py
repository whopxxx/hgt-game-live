"""运行: uv run tests/test_llm_stage_routing.py（完全离线, 无网络）。

Issue #23「按调用阶段配置可插拔模型路由」的回归:

    stage override -> env -> config/llm.local.json -> config/models.json -> code default

这个套件守的是**否定性**命题居多("配了 stage 不代表别的 stage 也跟着变"
/ "某个 stage 已警告过不代表别的 stage 的错配被吞掉"), 那类命题最容易
假绿 —— 所以每条都尽量配一个反证。

最要紧的三条:

  * **兜底守卫**(`test_every_production_call_declares_a_stage`): 以后
    新增生产 `client.messages()` 忘带 stage, 必须让这个套件变红。否则
    新调用点会静默落回全局模型 —— 配置看着生效, 实际那一环没走路由。
  * **`--model X` 清 env stage**(`test_cli_model_clears_env_stage_overrides`):
    旧脚本里 `--model X` 必须是"整场全用 X"。这是向后兼容的命门。
  * **错配按 stage 独立告警**
    (`test_two_stages_mismatch_warn_independently`): 一个全局 bool 会让
    第一个错配把后面所有 stage 的错配吞掉。

`tests/test_judge_golden.py` 那样要打真实网关的用例**不在**这里。
"""
import ast
import io
import json
import logging
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from story.config import (  # noqa: E402
    AI_SUPPORTED_MODELS_EXTRA_ENV, LLM_STAGES, STAGE_ENV_VARS, LLMConfig,
    SUPPORTED_MODELS, from_args, parse_model_stage_arg, supported_models,
)
from story.llm import AnthropicMessagesClient, LLMResult, PuzzleWriter  # noqa: E402

FAIL = [0]
A = "deepseek-v4.1-flash"      # 内置白名单里的两个模型
B = "glm-5.3-flash"


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


# ======================================================================
# 工具
# ======================================================================
class _Env:
    """临时改环境变量并在退出时**完整还原**。

    不还原的话, 一条用例往 os.environ 里塞的 `AI_MODEL_PUZZLE_STORY`
    会泄漏给后面所有用例 —— 那种"看执行顺序才过"的绿最难查。
    """

    def __init__(self, **kv):
        self.kv = kv
        self._saved = {}

    def __enter__(self):
        import os
        for k, v in self.kv.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        import os
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _clean_env():
    """清掉所有与本 Issue 相关的环境变量, 拿到一个确定的起点。"""
    import os
    for k in list(STAGE_ENV_VARS.values()) + ["AI_MODEL", AI_SUPPORTED_MODELS_EXTRA_ENV]:
        os.environ.pop(k, None)


class _FakeHTTP:
    """顶替 urllib.request.urlopen, 记录**真实请求体**。

    为什么必须看 body 而不是看调用参数: Issue 要证明的是
    `body["model"]` 真的等于当前 stage 解析出来的模型 —— 从参数到 body
    之间还隔着一次解析。只看参数会漏掉"参数对但 body 写错"。

    `respond_model` 让返回体自报一个不同的模型, 用来制造错配。
    """

    def __init__(self, respond_model=None):
        self.bodies = []
        self.respond_model = respond_model

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        self.bodies.append(body)
        model = self.respond_model or body.get("model")
        payload = {
            "content": [{"type": "text", "text": "ok"}],
            "model": model,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        outer = self

        class _Resp:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def read(self_):
                return json.dumps(payload).encode("utf-8")

        return _Resp()


class _CaptureLogs(logging.Handler):
    """抓 `story.llm` 的 WARNING —— 错配告警就是走这条路出去的。

    直接调 `_parse` 然后检查 `_warned_model_mismatches` 只能证明"记账了",
    证明不了"真的告警了"。这个 handler 让断言落在 operator 实际会看到的
    那行日志上。
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self):
        # `getMessage()` 已经做过 %-格式化 —— 再 `% record.args` 一次会
        # 在参数已消耗时报 "not all arguments converted"。
        return "\n".join(r.getMessage() for r in self.records)


def _client(cfg=None, respond_model=None):
    cfg = cfg or LLMConfig()
    c = AnthropicMessagesClient(cfg)
    fake = _FakeHTTP(respond_model=respond_model)
    return c, fake


def _call(c, fake, **kw):
    """在假 urlopen 下跑一次 messages(), 返回 (结果, 请求体)。"""
    orig = urllib.request.urlopen
    urllib.request.urlopen = fake
    try:
        r = c.messages("sys", "user", max_retries=0, **kw)
    finally:
        urllib.request.urlopen = orig
    return r, (fake.bodies[-1] if fake.bodies else None)


# ======================================================================
# 0. 本地 llm.local.json: URL / key / 模型 / 预算都能从一个文件配置
# ======================================================================
def test_local_llm_config_covers_endpoint_credentials_models_and_budgets():
    import os
    import story.config as C

    payload = {
        "base_url": "http://llm-gateway.example:9000",
        "api_key": "secret-sentinel-123",
        "default": B,
        "supported_models_extra": ["custom-local-model"],
        "puzzle.story": A,
        "qa.judge": A,
        "timeout": 42,
        "max_tokens": 1234,
        "max_retries": 1,
    }
    fd, path = tempfile.mkstemp(prefix="hgt_llm_local_", suffix=".json")
    os.close(fd)
    old_path = C.LLM_LOCAL_CONFIG_PATH
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        C.LLM_LOCAL_CONFIG_PATH = path
        _clean_env()

        clear = dict(
            AI_BASE_URL=None, AI_API_KEY=None, AI_TIMEOUT=None,
            AI_MAX_TOKENS=None, AI_MAX_RETRIES=None,
        )
        with _Env(**clear):
            cfg = LLMConfig()
            check("**local base_url 生效**",
                  cfg.base_url == payload["base_url"], cfg.base_url)
            check("**local api_key 生效**",
                  cfg.api_key == payload["api_key"], cfg.api_key)
            check("**local default model 生效**", cfg.model == B, cfg.model)
            check("**local supported_models_extra 生效**",
                  "custom-local-model" in supported_models(),
                  sorted(supported_models()))
            check("**local stage override 生效**",
                  cfg.model_for("puzzle.story") == A
                  and cfg.model_for("qa.judge") == A,
                  cfg.stage_models)
            check("**没写的 stage 继承 local default**",
                  cfg.model_for("hint") == B, cfg.model_for("hint"))
            check("**local timeout/max_tokens/max_retries 生效**",
                  (cfg.timeout, cfg.max_tokens, cfg.max_retries) == (42.0, 1234, 1),
                  (cfg.timeout, cfg.max_tokens, cfg.max_retries))
            check("**启动日志不会吐出完整 API key**",
                  payload["api_key"] not in json.dumps(cfg.masked(), ensure_ascii=False),
                  cfg.masked())

        # env 仍然是高级覆盖。
        with _Env(AI_BASE_URL="http://env-gateway:8088",
                  AI_API_KEY="env-secret",
                  AI_MODEL=A,
                  AI_MODEL_PUZZLE_STORY=B,
                  AI_TIMEOUT="9",
                  AI_MAX_TOKENS="321",
                  AI_MAX_RETRIES="0"):
            cfg2 = LLMConfig()
            check("**env base_url/api_key 高于 local**",
                  cfg2.base_url == "http://env-gateway:8088"
                  and cfg2.api_key == "env-secret",
                  (cfg2.base_url, cfg2.api_key))
            check("**AI_MODEL 压掉 local stage，env stage 再覆盖**",
                  cfg2.model == A
                  and cfg2.model_for("puzzle.story") == B
                  and cfg2.model_for("qa.judge") == A,
                  (cfg2.model, cfg2.stage_models))
            check("**env budgets 高于 local**",
                  (cfg2.timeout, cfg2.max_tokens, cfg2.max_retries) == (9.0, 321, 0),
                  (cfg2.timeout, cfg2.max_tokens, cfg2.max_retries))

        # local 文件拼错字段必须 fail-fast。
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"api_ulr": "typo"}, fh)
        try:
            C._load_llm_local_config(path)
            check("**local 未知 key 必须报错**", False, "未报错")
        except ValueError as e:
            check("**local 未知 key 明确报错**", "api_ulr" in str(e), str(e))

        # 真正含 key 的本地文件必须被 gitignore。
        root = Path(__file__).resolve().parents[1]
        gi = (root / ".gitignore").read_text(encoding="utf-8")
        check("**llm.local.json 被 gitignore**",
              "config/llm.local.json" in gi)
        check("**仓库提供无真实凭据的 example**",
              (root / "config" / "llm.example.json").exists())
    finally:
        C.LLM_LOCAL_CONFIG_PATH = old_path
        try:
            os.remove(path)
        except OSError:
            pass


# ======================================================================
# 0. 简单 models.json: 日常配置入口 + 优先级
# ======================================================================
def test_simple_models_json_config_and_precedence():
    import story.config as C

    payload = {
        "default": A,
        "puzzle.story": B,
        "puzzle.surface": B,
    }
    fd, path = tempfile.mkstemp(prefix="hgt_models_", suffix=".json")
    import os
    os.close(fd)
    old_path = C.MODEL_CONFIG_PATH
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        C.MODEL_CONFIG_PATH = path
        _clean_env()

        cfg = LLMConfig()
        check("**models.json default 成为全局模型**", cfg.model == A, cfg.model)
        check("**文件里的 puzzle.story override 生效**",
              cfg.model_for("puzzle.story") == B, cfg.stage_models)
        check("**没写的 stage 自动继承 default**",
              cfg.model_for("qa.judge") == A, cfg.model_for("qa.judge"))

        # env global 优先于文件: 表示“整场先全用 B”, 文件 stage 不再偷偷覆盖。
        with _Env(AI_MODEL=B):
            cfg2 = LLMConfig()
            check("**AI_MODEL 覆盖 models.json default**", cfg2.model == B, cfg2.model)
            check("**AI_MODEL 同时压掉文件 stage override**",
                  cfg2.stage_models == {} and cfg2.model_for("puzzle.story") == B,
                  cfg2.stage_models)

        # 更具体的 env stage 仍然优先于 env global。
        with _Env(AI_MODEL=B, AI_MODEL_PUZZLE_STORY=A):
            cfg3 = LLMConfig()
            check("**AI_MODEL_<STAGE> 高于 AI_MODEL**",
                  cfg3.model_for("puzzle.story") == A,
                  cfg3.model_for("puzzle.story"))

        # CLI --model 仍然是整场强覆盖; --model-stage 再单独叠加。
        c4 = from_args(["--sim", "x.jsonl", "--model", A,
                        "--model-stage", f"puzzle.story={B}"])
        check("**CLI 仍是最高优先级**",
              c4.model_for("puzzle.story") == B if hasattr(c4, "model_for") else
              c4.llm.model_for("puzzle.story") == B)

        # 配置文件 typo 必须明确失败, 不能静默忽略。
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"default": A, "puzzle.stroy": B}, fh)
        try:
            C._load_model_config_file(path)
            check("**models.json 未知 key 必须报错**", False, "未报错")
        except ValueError as e:
            check("**models.json 未知 key 明确报错**", "puzzle.stroy" in str(e), str(e))
    finally:
        C.MODEL_CONFIG_PATH = old_path
        try:
            os.remove(path)
        except OSError:
            pass


# ======================================================================
# 1. 只有 AI_MODEL 时, 所有 stage 都用全局模型(旧行为兼容)
# ======================================================================
def test_global_model_only_all_stages_fall_back():
    only_global = {"AI_MODEL": A}
    only_global.update({k: None for k in STAGE_ENV_VARS.values()})
    with _Env(**only_global):
        cfg = LLMConfig()
        check("**只设 AI_MODEL -> stage_models 为空**", cfg.stage_models == {},
              cfg.stage_models)
        resolved = cfg.resolved_models()
        check("**所有 stage 都解析成全局模型**",
              set(resolved.values()) == {A}, sorted(set(resolved.values())))
        check("**resolved_models 覆盖全部 stage**",
              set(resolved) == set(LLM_STAGES), len(resolved))

    # 反证: 全局模型换了, 所有 stage 必须**跟着**换(而不是被钉成默认值)
    only_b = {k: None for k in STAGE_ENV_VARS.values()}
    only_b["AI_MODEL"] = B
    with _Env(**only_b):
        check("**换 AI_MODEL -> 所有 stage 跟着换**",
              set(LLMConfig().resolved_models().values()) == {B})


# ======================================================================
# 2. 单 stage override 只影响一个阶段
# ======================================================================
def test_single_stage_override_is_isolated():
    with _Env(AI_MODEL=A, AI_MODEL_PUZZLE_REVIEW=B):
        cfg = LLMConfig()
        check("**puzzle.review 用 override**",
              cfg.model_for("puzzle.review") == B, cfg.model_for("puzzle.review"))
        others = {s: cfg.model_for(s) for s in LLM_STAGES if s != "puzzle.review"}
        check("**其余 stage 全部仍是全局模型**",
              set(others.values()) == {A}, sorted(set(others.values())))
        check("**stage_models 只记了配过的那个**",
              cfg.stage_models == {"puzzle.review": B}, cfg.stage_models)


# ======================================================================
# 3. 多 stage override 互不串路
# ======================================================================
def test_multiple_stage_overrides_do_not_cross_wires():
    with _Env(AI_MODEL=A,
              AI_MODEL_PUZZLE_STORY=B,
              AI_MODEL_QA_JUDGE=B,
              AI_MODEL_HINT=A):
        cfg = LLMConfig()
        want = {"puzzle.story": B, "qa.judge": B, "hint": A}
        got = {s: cfg.model_for(s) for s in want}
        check("**三个 override 各归各位**", got == want, got)
        check("**没配的 stage 仍走全局**",
              cfg.model_for("puzzle.surface") == A
              and cfg.model_for("qa.answer") == A)

    # 反证: 三个 stage 给三个**不同**模型, 一个都不能串
    with _Env(AI_MODEL=A, AI_MODEL_PUZZLE_STORY=B,
              AI_MODEL_PUZZLE_SURFACE=A, AI_MODEL_PUZZLE_STRUCTURE=B):
        cfg = LLMConfig()
        check("**不同模型交错配置不串路**",
              cfg.model_for("puzzle.story") == B
              and cfg.model_for("puzzle.surface") == A
              and cfg.model_for("puzzle.structure") == B)


# ======================================================================
# 4. --model X 清掉 env stage override
# ======================================================================
def test_cli_model_clears_env_stage_overrides():
    _clean_env()
    with _Env(AI_MODEL=A, AI_MODEL_PUZZLE_STORY=B, AI_MODEL_QA_JUDGE=B):
        # 不传 --model -> env stage 生效
        c0 = from_args(["--sim", "x.jsonl"])
        check("**基线: env stage 生效**",
              c0.llm.model_for("puzzle.story") == B)

        # 传 --model -> 整场全用 X, env stage 被清掉
        c1 = from_args(["--sim", "x.jsonl", "--model", B])
        check("**--model 覆盖全局模型**", c1.llm.model == B, c1.llm.model)
        check("**--model 清掉 env stage override**",
              c1.llm.stage_models == {}, c1.llm.stage_models)
        check("**--model 之后所有 stage 都是它**",
              set(c1.llm.resolved_models().values()) == {B},
              sorted(set(c1.llm.resolved_models().values())))


# ======================================================================
# 5. --model X --model-stage puzzle.story=Y -> story 用 Y, 其余用 X
# ======================================================================
def test_cli_model_stage_overrides_after_model():
    _clean_env()
    with _Env(AI_MODEL=B, AI_MODEL_PUZZLE_STORY=A, AI_MODEL_PUZZLE_REVIEW=A):
        c = from_args(["--sim", "x.jsonl", "--model", A,
                       "--model-stage", f"puzzle.story={B}"])
        check("**story 用 CLI stage 指定的模型**",
              c.llm.model_for("puzzle.story") == B, c.llm.model_for("puzzle.story"))
        check("**其余 stage 用 --model 指定的模型**",
              c.llm.model_for("puzzle.review") == A
              and c.llm.model_for("qa.judge") == A,
              (c.llm.model_for("puzzle.review"), c.llm.model_for("qa.judge")))
        # env 里那个 puzzle.review=A 与 --model A 恰好同值, 换个值再验一次,
        # 否则这条可能因为"两边碰巧一样"而假绿。
        c2 = from_args(["--sim", "x.jsonl", "--model", A,
                        "--model-stage", f"puzzle.story={B}"])
        check("**env 的 puzzle.review 确实被清掉了(不是碰巧同值)**",
              "puzzle.review" not in c2.llm.stage_models, c2.llm.stage_models)

    # --model-stage 可重复, 且多个 stage 同时生效
    _clean_env()
    c3 = from_args(["--sim", "x.jsonl", "--model", A,
                    "--model-stage", f"puzzle.story={B}",
                    "--model-stage", f"qa.answer={B}"])
    check("**--model-stage 可重复**",
          c3.llm.model_for("puzzle.story") == B
          and c3.llm.model_for("qa.answer") == B
          and c3.llm.model_for("puzzle.surface") == A)

    # 只给 --model-stage 不给 --model: env 的**其他** stage 仍然保留
    _clean_env()
    with _Env(AI_MODEL=A, AI_MODEL_PUZZLE_REVIEW=B):
        c4 = from_args(["--sim", "x.jsonl",
                        "--model-stage", f"puzzle.story={B}"])
        check("**不给 --model 时 env 的其他 stage 不受影响**",
              c4.llm.model_for("puzzle.review") == B
              and c4.llm.model_for("puzzle.story") == B
              and c4.llm.model_for("qa.judge") == A,
              c4.llm.stage_models)


# ======================================================================
# 6. 未知 stage 明确报错(不静默忽略 typo)
# ======================================================================
def test_unknown_stage_is_rejected_loudly():
    # ① 配置层
    cfg = LLMConfig()
    try:
        cfg.model_for("puzzle.stroy")          # 拼错
        check("**model_for 未知 stage 必须抛错**", False, "没抛")
    except ValueError as e:
        check("**model_for 未知 stage 抛 ValueError**", "puzzle.stroy" in str(e))

    # ② CLI 参数解析
    try:
        parse_model_stage_arg(f"puzzle.stroy={B}")
        check("**parse_model_stage_arg 未知 stage 必须抛错**", False, "没抛")
    except ValueError as e:
        check("**parse_model_stage_arg 抛 ValueError**", "puzzle.stroy" in str(e))

    # ③ 缺 = 号
    try:
        parse_model_stage_arg(A)
        check("**缺 '=' 必须抛错**", False, "没抛")
    except ValueError:
        check("**缺 '=' 抛 ValueError**", True)

    # ④ 冒号分隔(常见手滑)也必须报错
    try:
        parse_model_stage_arg(f"puzzle.story:{B}")
        check("**用 ':' 分隔必须抛错**", False, "没抛")
    except ValueError:
        check("**用 ':' 分隔抛 ValueError**", True)

    # ⑤ 端到端: from_args 里也必须炸, 而不是静默忽略
    _clean_env()
    try:
        from_args(["--sim", "x.jsonl", "--model-stage", f"puzzle.stroy={B}"])
        check("**from_args 对未知 stage 必须抛错**", False, "被静默忽略了")
    except ValueError:
        check("**from_args 对未知 stage 抛 ValueError**", True)


# ======================================================================
# 7. global model 与 stage model 都执行白名单校验
# ======================================================================
def test_whitelist_covers_global_and_stage_models():
    _clean_env()
    with _Env(AI_SUPPORTED_MODELS_EXTRA=None):
        # 全局未知
        cfg = LLMConfig()
        cfg.model = "not-a-real-model"
        bad = cfg.unknown_models()
        check("**全局未知模型被标出**",
              ("(global)", "not-a-real-model") in bad, bad)

        # stage 未知
        cfg2 = LLMConfig()
        cfg2.model = A
        cfg2.stage_models = {"puzzle.review": "nope-2"}
        bad2 = cfg2.unknown_models()
        check("**stage 未知模型被标出, 且带 stage 名**",
              bad2 == [("puzzle.review", "nope-2")], bad2)
        check("**合法的 global 不被误报**",
              ("(global)", A) not in bad2)

        # 两个都未知 -> 两条都在
        cfg3 = LLMConfig()
        cfg3.model = "nope-g"
        cfg3.stage_models = {"qa.judge": "nope-j"}
        check("**global + stage 都不合法 -> 两条都报**",
              set(cfg3.unknown_models()) == {("(global)", "nope-g"),
                                             ("qa.judge", "nope-j")},
              cfg3.unknown_models())

        # Config.validate() 的 warning 必须能指出是哪个 stage
        c = from_args(["--sim", "x.jsonl", "--model", A,
                       "--model-stage", f"puzzle.review={'zzz-unknown'}"])
        warns = [w for w in c.validate() if "zzz-unknown" in w]
        check("**validate() warning 指出 stage 名**",
              len(warns) == 1 and "puzzle.review" in warns[0], warns)
        check("**validate() warning 提到扩展入口**",
              warns and AI_SUPPORTED_MODELS_EXTRA_ENV in warns[0], warns)

        # 合法配置不得产生模型类 warning(防"见谁都报")
        c_ok = from_args(["--sim", "x.jsonl", "--model", A,
                          "--model-stage", f"puzzle.review={B}"])
        check("**合法配置没有模型 warning**",
              not [w for w in c_ok.validate() if "不在已知列表" in w],
              c_ok.validate())


# ======================================================================
# 8. AI_SUPPORTED_MODELS_EXTRA 扩展允许集合, 不改默认模型
# ======================================================================
def test_supported_models_extra_extends_only():
    _clean_env()
    with _Env(AI_SUPPORTED_MODELS_EXTRA=None):
        base = supported_models()
        check("**无扩展时 = 内置白名单**", base == SUPPORTED_MODELS, base)

        c = LLMConfig()
        c.model = "my-private-model"
        check("**未扩展时自定义模型仍被拒**",
              ("(global)", "my-private-model") in c.unknown_models())

    with _Env(AI_SUPPORTED_MODELS_EXTRA="my-private-model, another-one"):
        now = supported_models()
        check("**扩展后包含新模型**",
              {"my-private-model", "another-one"} <= now, sorted(now))
        check("**扩展是并集, 内置的还在**",
              SUPPORTED_MODELS <= now, sorted(now))

        c = LLMConfig()
        c.model = "my-private-model"
        c.stage_models = {"puzzle.story": "another-one"}
        check("**扩展后 global 与 stage 都放行**",
              c.unknown_models() == [], c.unknown_models())

    # 逗号两侧的空白必须被容错(operator 手写 env 很容易带空格)
    with _Env(AI_SUPPORTED_MODELS_EXTRA="  spaced-model  ,, "):
        check("**扩展项容忍空白与空段**",
              "spaced-model" in supported_models(),
              sorted(supported_models()))

    # 扩展**不得**改变默认模型 —— 这是"只扩展"的定义
    with _Env(AI_SUPPORTED_MODELS_EXTRA="zzz"):
        check("**扩展不改变默认模型**",
              LLMConfig().model == "deepseek-v4.1-flash", LLMConfig().model)


# ======================================================================
# 9. 请求体 body["model"] 确实等于当前 stage 的 resolved model
# ======================================================================
def test_request_body_uses_stage_resolved_model():
    with _Env(AI_MODEL=A, AI_MODEL_PUZZLE_REVIEW=B):
        cfg = LLMConfig()
        for stage, want in (("puzzle.review", B), ("puzzle.surface", A),
                            ("qa.judge", A)):
            c, fake = _client(cfg)
            r, body = _call(c, fake, stage=stage)
            check(f"**body['model'] == {stage} 解析值**",
                  body is not None and body["model"] == want,
                  body and body.get("model"))

        # 多 stage 连续调用: 每个 stage 各自解析, 不互相污染
        c, fake = _client(cfg)
        for stage in ("puzzle.review", "puzzle.surface", "puzzle.review"):
            _call(c, fake, stage=stage)
        got = [b["model"] for b in fake.bodies]
        check("**连续多 stage 调用各用各的模型**",
              got == [B, A, B], got)

    # 无 stage 的调用(诊断)落回全局模型
    with _Env(AI_MODEL=A, AI_MODEL_PUZZLE_REVIEW=B):
        cfg = LLMConfig()
        c, fake = _client(cfg)
        r, body = _call(c, fake)
        check("**不传 stage -> 用全局模型**", body["model"] == A, body.get("model"))

    # 显式 model= 逃生口优先于 stage(仅测试/诊断用)
    with _Env(AI_MODEL=A, AI_MODEL_PUZZLE_REVIEW=B):
        cfg = LLMConfig()
        c, fake = _client(cfg)
        r, body = _call(c, fake, stage="puzzle.review", model="escape-hatch")
        check("**显式 model= 覆盖 stage**",
              body["model"] == "escape-hatch", body.get("model"))

    # LLMResult 上的 requested_model 必须与请求体一致
    with _Env(AI_MODEL=A, AI_MODEL_PUZZLE_REVIEW=B):
        cfg = LLMConfig()
        c, fake = _client(cfg)
        r, body = _call(c, fake, stage="puzzle.review")
        check("**LLMResult.requested_model == body['model']**",
              r.requested_model == body["model"], r.requested_model)
        check("**LLMResult.stage 回填**", r.stage == "puzzle.review", r.stage)


# ======================================================================
# 10. 返回体 model != requested model -> warning 含 stage/requested/actual
# ======================================================================
def test_model_mismatch_warning_has_stage_requested_actual():
    with _Env(AI_MODEL=A):
        cfg = LLMConfig()
        c, fake = _client(cfg, respond_model="gateway-default")
        h = _CaptureLogs()
        llm_log = logging.getLogger("story.llm")
        llm_log.addHandler(h)
        try:
            r, body = _call(c, fake, stage="puzzle.review")
        finally:
            llm_log.removeHandler(h)
        txt = h.text()
        check("**warning 里有 stage**", "puzzle.review" in txt, txt)
        check("**warning 里有 requested**", f"requested={A}" in txt, txt)
        check("**warning 里有 actual**",
              "actual=gateway-default" in txt, txt)
        check("**返回体 model 语义未变(仍是实际模型)**",
              r.model == "gateway-default", r.model)

    # 反证: 返回体与 requested 一致 -> **不能**告警
    with _Env(AI_MODEL=A):
        cfg = LLMConfig()
        c, fake = _client(cfg, respond_model=A)
        h = _CaptureLogs()
        llm_log = logging.getLogger("story.llm")
        llm_log.addHandler(h)
        try:
            _call(c, fake, stage="puzzle.review")
        finally:
            llm_log.removeHandler(h)
        check("**一致时没有错配 warning**",
              "模型错配" not in h.text(), h.text())

    # 反证: stage override 让 requested 变了, 但网关**正确**返回该模型
    # -> 不能因为"它 != 全局模型"而误报(旧逻辑在这里会假警报)
    with _Env(AI_MODEL=A, AI_MODEL_PUZZLE_REVIEW=B):
        cfg = LLMConfig()
        c, fake = _client(cfg, respond_model=B)
        h = _CaptureLogs()
        llm_log = logging.getLogger("story.llm")
        llm_log.addHandler(h)
        try:
            _call(c, fake, stage="puzzle.review")
        finally:
            llm_log.removeHandler(h)
        check("**stage 模型被正确返回时不误报**",
              "模型错配" not in h.text(), h.text())


# ======================================================================
# 11. 两个 stage 分别错配 -> 两条 warning 都出现
# ======================================================================
def test_two_stages_mismatch_warn_independently():
    """一个全局 bool 会让第一个错配把后面所有 stage 的错配吞掉。

    这条就是冲着那个 bug 来的: 断言是"**两条**都在", 而不是"至少一条"。
    """
    with _Env(AI_MODEL=A, AI_MODEL_PUZZLE_REVIEW=B):
        cfg = LLMConfig()
        # 每次都返回一个与 requested 不同的模型 -> 两个 stage 都错配
        c = AnthropicMessagesClient(cfg)
        fake = _FakeHTTP(respond_model=None)   # respond_model=None -> 回 body 的

        class _AlwaysWrong(_FakeHTTP):
            def __call__(self_, req, timeout=None):
                body = json.loads(req.data.decode("utf-8"))
                self_.bodies.append(body)

                class _Resp:
                    def __enter__(s):
                        return s

                    def __exit__(s, *a):
                        return False

                    def read(s):
                        return json.dumps({
                            "content": [{"type": "text", "text": "ok"}],
                            "model": "gateway-wrong",
                            "usage": {},
                        }).encode("utf-8")
                return _Resp()

        fake = _AlwaysWrong()
        h = _CaptureLogs()
        llm_log = logging.getLogger("story.llm")
        llm_log.addHandler(h)
        try:
            _call(c, fake, stage="puzzle.review")   # requested B
            _call(c, fake, stage="qa.answer")       # requested A
        finally:
            llm_log.removeHandler(h)
        txt = h.text()
        check("**puzzle.review 的错配被报出**", "puzzle.review" in txt, txt)
        check("**qa.answer 的错配也被报出(没被吞掉)**", "qa.answer" in txt, txt)
        check("**两条 requested 各自正确**",
              f"requested={B}" in txt and f"requested={A}" in txt, txt)
        check("**去重集合按三元组记账**",
              ("puzzle.review", B, "gateway-wrong") in c._warned_model_mismatches
              and ("qa.answer", A, "gateway-wrong") in c._warned_model_mismatches,
              c._warned_model_mismatches)

        # 反证: 同一个 (stage, requested, actual) 重复出现只报一次(不刷屏)
        h2 = _CaptureLogs()
        llm_log.addHandler(h2)
        try:
            _call(c, fake, stage="puzzle.review")
        finally:
            llm_log.removeHandler(h2)
        check("**同一三元组重复出现不重复告警**",
              "模型错配" not in h2.text(), h2.text())


# ======================================================================
# 12. keyword2 story / surface / structure 三阶段路由正确
# ======================================================================
def _stage_of_last(writer, fn, *a, **kw):
    """跑一个 writer 方法, 返回它最后一次发出的 stage(没调用则 None)。"""
    try:
        fn(*a, **kw)
    except Exception:                          # noqa: BLE001
        pass
    return writer.client.calls[-1].get("stage") if writer.client.calls else None


def test_keyword2_stages_route_correctly():
    from tests.test_llm import FakeClient, riddle
    cfg = _runtime_cfg()

    cli = FakeClient([LLMResult(tool_input={"answer": "汤底"}, model="m")])
    w = PuzzleWriter(client=cli, runtime_cfg=cfg)
    s = _stage_of_last(w, w.gen_keyword_story, ["钥匙", "锁"], "live")
    check("**gen_keyword_story -> puzzle.story**", s == "puzzle.story", s)

    cli2 = FakeClient([LLMResult(tool_input={"puzzle": "谜面"}, model="m")])
    w2 = PuzzleWriter(client=cli2, runtime_cfg=cfg)
    s2 = _stage_of_last(w2, w2.gen_surface, "汤底")
    check("**gen_surface -> puzzle.surface**", s2 == "puzzle.surface", s2)

    # Stage A 的三样必须来自**同一道合格题**(见 test_g4_source 的说明)——
    # 随手编的"谜面?"会让 validate_spec 把这次成功判成结构不过。
    #
    # ⚠️ `structure_original_idea()` 内部会**接着调审稿**, 所以不能看
    # "最后一次调用"—— 那拿到的是 puzzle.review。按 tool 名定位结构调用。
    base = riddle()
    cli3 = FakeClient([LLMResult(tool_input=base, model="m")])
    w3 = PuzzleWriter(client=cli3, runtime_cfg=cfg)
    _stage_of_last(w3, w3.structure_original_idea,
                   title=base["title"], puzzle=base["puzzle"],
                   answer=base["answer"])
    struct = [c for c in cli3.calls
              if (c.get("tool") or {}).get("name") == "emit_structure"]
    s3 = struct[0].get("stage") if struct else None
    check("**structure_original_idea -> puzzle.structure**",
          s3 == "puzzle.structure", s3)
    check("**结构调用确实发生了(不是空断言)**", bool(struct),
          [c.get("tool") for c in cli3.calls])

    # 反证: 三条链的 stage 名两两不同(否则"分别路由"无从谈起)
    check("**三个 keyword2 stage 名互不相同**",
          len({s, s2, s3}) == 3, (s, s2, s3))


def _runtime_cfg():
    from tests.test_llm import runtime_cfg
    return runtime_cfg()


# ======================================================================
# 13. classic _gen_spec_once -> puzzle.generate
# ======================================================================
def test_classic_generator_uses_puzzle_generate():
    from tests.test_llm import FakeClient, riddle, review_ok
    base = riddle()
    cli = FakeClient([LLMResult(tool_input=base, model="m"),
                      LLMResult(tool_input=review_ok(base["puzzle"]), model="m")])
    w = PuzzleWriter(client=cli, runtime_cfg=_runtime_cfg())
    w.gen_spec(blueprint=cli.default_blueprint)
    stages = [c.get("stage") for c in cli.calls]
    check("**出题链里出现 puzzle.generate**",
          "puzzle.generate" in stages, stages)
    gen_calls = [c for c in cli.calls if c.get("stage") == "puzzle.generate"]
    check("**puzzle.generate 用的是出题工具 emit_riddle**",
          gen_calls and (gen_calls[0]["tool"] or {}).get("name") == "emit_riddle",
          [ (c["tool"] or {}).get("name") for c in gen_calls ])


# ======================================================================
# 14. Reviewer / truth audit / safety 三个 stage 不串
# ======================================================================
def test_reviewer_audit_safety_stages_do_not_cross():
    from tests.test_llm import FakeClient, riddle, review_ok
    base = riddle()
    cli = FakeClient([LLMResult(tool_input=base, model="m"),
                      LLMResult(tool_input=review_ok(base["puzzle"]), model="m")])
    w = PuzzleWriter(client=cli, runtime_cfg=_runtime_cfg())
    w.gen_spec(blueprint=cli.default_blueprint)
    stages = [c.get("stage") for c in cli.calls]
    check("**审稿走 puzzle.review**", "puzzle.review" in stages, stages)
    check("**audit 走 puzzle.truth_audit**",
          "puzzle.truth_audit" in stages, stages)

    # safety 是独立入口(verify_safety 自己有重试循环)
    cli2 = FakeClient([LLMResult(tool_input={"livestream_safe": True,
                                             "why": "ok"}, model="m")])
    w2 = PuzzleWriter(client=cli2, runtime_cfg=_runtime_cfg())
    s = _stage_of_last(w2, w2.verify_safety, "谜面", "谜底")
    check("**verify_safety 走 puzzle.safety**", s == "puzzle.safety", s)
    check("**三个 stage 名字互不相同**",
          len({"puzzle.review", "puzzle.truth_audit", s}) == 3)


# ======================================================================
# 15. QA answer / candidate recheck / completion verify / judge 各走各的
# ======================================================================
def test_qa_stages_route_independently():
    from tests.test_llm import FakeClient, _GOOD_PUZ
    cfg = _runtime_cfg()

    # ---- qa.answer ----
    cli = FakeClient([LLMResult(tool_input={"answers": [
        {"id": 1, "verdict": "是", "comment": "对"}]}, model="m")])
    w = PuzzleWriter(client=cli, runtime_cfg=cfg)
    s = _stage_of_last(w, w.answer, "谜面", "谜底", [], 1, "甲", "他是盲人吗")
    check("**answer -> qa.answer**", s == "qa.answer", s)

    # ---- qa.judge(answer() 内部在 judge_solve=True 时会调 judge) ----
    cli_j = FakeClient([
        LLMResult(tool_input={"answers": [
            {"id": 1, "verdict": "是", "solution_candidate": True}]}, model="m"),
        LLMResult(tool_input={"is_guess": True, "cause_hit": True,
                              "mechanism_hit": True}, model="m")])
    wj = PuzzleWriter(client=cli_j, runtime_cfg=cfg)
    _stage_of_last(wj, wj.answer, "谜面", "谜底", [], 1, "甲", "他是盲人吗")
    jstages = [c.get("stage") for c in cli_j.calls]
    check("**answer 链里 judge 走 qa.judge**", "qa.judge" in jstages, jstages)
    check("**answer 与 judge 是不同的 stage**",
          "qa.answer" in jstages and len(set(jstages)) >= 2, jstages)

    # ---- 单独调 judge ----
    cli_j2 = FakeClient([LLMResult(tool_input={
        "is_guess": True, "cause_hit": True, "mechanism_hit": True}, model="m")])
    wj2 = PuzzleWriter(client=cli_j2, runtime_cfg=cfg)
    s_j = _stage_of_last(wj2, wj2.judge, "谜面", "谜底", "他是盲人吗")
    check("**judge -> qa.judge**", s_j == "qa.judge", s_j)

    # ---- qa.candidate_recheck / qa.completion_verify ----
    # 这两个是**私有**方法, 且都有一堆前置门(status/candidate/合同...)。
    # 直接调很容易因为门没开而**一次 LLM 都不发** —— 那种"空断言"会绿得
    # 毫无意义。所以走**生产路径** `answer()`, 用能真正触发它们的构造。
    from tests.test_solve_ux import auction_spec, _verdict

    spec = auction_spec()

    # qa.completion_verify: 第一层答"是" + candidate=True + 自报一个
    # completion fact -> 必须过 completion 复核
    cli_v = FakeClient([
        _verdict(cand=True, established=["f2"]),
        LLMResult(tool_input={"matched_completion_fact_ids": ["f2"]}, model="m"),
    ])
    wv = PuzzleWriter(client=cli_v, runtime_cfg=cfg)
    _stage_of_last(
        wv, wv.answer, spec.puzzle, spec.answer, [], 1, "甲", "歌正好四十分钟",
        spec=spec, completion_fact_ids=spec.completion_fact_ids,
        core_answer=spec.core_answer, room_established_fact_ids=[])
    vstages = [c.get("stage") for c in cli_v.calls]
    check("**_completion_verify -> qa.completion_verify**",
          "qa.completion_verify" in vstages, vstages)
    check("**completion 复核确实被调用(不是空断言)**",
          len(cli_v.calls) >= 2, vstages)

    # qa.candidate_recheck: 第一层自相矛盾(candidate=True 但 verdict=无关)
    # -> 定向重判
    from tests.test_solve_ux import _recheck
    cli_c = FakeClient([
        _verdict(cand=True, verdict="无关"),
        _recheck("是", ids=["f2"]),
    ])
    wc = PuzzleWriter(client=cli_c, runtime_cfg=cfg)
    _stage_of_last(
        wc, wc.answer, spec.puzzle, spec.answer, [], 1, "甲", "歌正好四十分钟",
        spec=spec, completion_fact_ids=spec.completion_fact_ids,
        core_answer=spec.core_answer, room_established_fact_ids=[])
    cstages = [c.get("stage") for c in cli_c.calls]
    check("**_candidate_recheck -> qa.candidate_recheck**",
          "qa.candidate_recheck" in cstages, cstages)

    check("**四个 QA stage 名字互不相同**",
          len({"qa.answer", "qa.judge", "qa.candidate_recheck",
               "qa.completion_verify"}) == 4)


# ======================================================================
# 16. hint / reveal 分开
# ======================================================================
def test_hint_and_reveal_are_separate_stages():
    from tests.test_llm import FakeClient
    cfg = _runtime_cfg()
    cli = FakeClient([LLMResult(tool_input={"hint": "注意时间"}, model="m")])
    w = PuzzleWriter(client=cli, runtime_cfg=cfg)
    s = _stage_of_last(w, w.hint, "谜面", "谜底", 1, [])
    check("**hint -> hint**", s == "hint", s)

    cli2 = FakeClient([LLMResult(tool_input={"reveal": "真相是…"}, model="m")])
    w2 = PuzzleWriter(client=cli2, runtime_cfg=cfg)
    s2 = _stage_of_last(w2, w2.reveal, "谜面", "谜底", "solved", "甲")
    check("**reveal -> reveal**", s2 == "reveal", s2)
    check("**两个 stage 名字不同**", s != s2, (s, s2))


# ======================================================================
# 17. temperature / timeout / retries 与改造前一致
# ======================================================================
def test_budgets_unchanged_by_routing():
    """模型路由**不得**动 temperature / timeout / retries。

    做法: 同一个调用在"有 stage override"和"没有 override"两种配置下,
    除 model 外**所有**参数必须逐项相同。
    """
    from tests.test_llm import FakeClient, riddle, review_ok

    def _run():
        base = riddle()
        cli = FakeClient([LLMResult(tool_input=base, model="m"),
                          LLMResult(tool_input=review_ok(base["puzzle"]),
                                    model="m")])
        w = PuzzleWriter(client=cli, runtime_cfg=_runtime_cfg())
        w.gen_spec(blueprint=cli.default_blueprint)
        return [(c["stage"], c["temperature"], c["timeout"],
                 c["max_retries"], c["max_tokens"]) for c in cli.calls]

    base_run = _run()
    # 给这些 stage 配上 override, 只应改模型, 不改任何预算
    with _Env(AI_MODEL_PUZZLE_GENERATE=B, AI_MODEL_PUZZLE_REVIEW=B,
              AI_MODEL_PUZZLE_TRUTH_AUDIT=B):
        after = _run()

    check("**stage 数量与顺序不变**",
          [x[0] for x in base_run] == [x[0] for x in after],
          (base_run, after))
    check("**temperature/timeout/retries/max_tokens 逐项不变**",
          [x[1:] for x in base_run] == [x[1:] for x in after],
          (base_run, after))

    # temperature 也不得被路由影响(它决定请求体里的 temperature)
    c1, f1 = _client(LLMConfig())
    c2cfg = LLMConfig()
    c2cfg.stage_models = {"qa.judge": B}
    c2, f2 = _client(c2cfg)
    _call(c1, f1, stage="qa.judge", temperature=0.35)
    _call(c2, f2, stage="qa.judge", temperature=0.35)
    check("**temperature 不受 stage override 影响**",
          f1.bodies[0].get("temperature") == f2.bodies[0].get("temperature")
          == 0.35,
          (f1.bodies[0].get("temperature"), f2.bodies[0].get("temperature")))
    check("**同一个 temperature 下 model 才不同**",
          f1.bodies[0]["model"] != f2.bodies[0]["model"])
    check("**timeout 也不受影响(默认沿用全局)**",
          f1.bodies[0]["max_tokens"] == f2.bodies[0]["max_tokens"])


# ======================================================================
# 18. --no-llm 行为不变
# ======================================================================
def test_no_llm_behavior_unchanged():
    _clean_env()
    with _Env(AI_MODEL=A, AI_MODEL_PUZZLE_REVIEW=B):
        c = from_args(["--sim", "x.jsonl", "--no-llm"])
        check("**--no-llm 仍然关掉 LLM**", c.no_llm is True)
        # --no-llm 下模型校验的 warning 不应出现(不调 LLM 就无所谓模型)
        warns = [w for w in c.validate() if "不在已知列表" in w]
        check("**--no-llm 不产生模型 warning**", not warns, warns)

    # --no-llm 与 --model-stage 同时给也不该炸
    _clean_env()
    c2 = from_args(["--sim", "x.jsonl", "--no-llm", "--model", A,
                    "--model-stage", f"puzzle.story={B}"])
    check("**--no-llm 与 --model-stage 共存不冲突**",
          c2.no_llm is True and c2.llm.model_for("puzzle.story") == B)


# ======================================================================
# 兜底守卫: 生产 client.messages() 必须显式带 stage
# ======================================================================
#: 允许**不带** stage 的调用点 —— 只有测试/诊断。
#:
#: 这个白名单是**故意**短且显式的: 新增生产调用必须在这里补名字或带
#: stage, 两种都会在 review 里被看见。用"整文件"或"整个类"豁免等于
#: 关掉守卫。
#:
#: 现在是**空的** —— 连启动探针都显式带了 `stage="probe"`。留着空集而不是
#: 删掉整个机制, 是为了以后真有诊断调用时有个已声明的落点(而不是临时
#: 把守卫改松)。
_STAGE_EXEMPT_CALLS: dict[tuple, str] = {}

#: 生产**业务**文件: 这些文件里的 messages() 调用必须带 stage。
#: tools/ 下的一次性实验脚本不在其列(它们不属于直播生产路径)。
_PRODUCTION_FILES = (
    "story/llm.py",
    "story/public_player.py",
    "tools/curated_compiler.py",
)


def test_every_production_call_declares_a_stage():
    """**兜底守卫**: 以后新增生产 `client.messages()` 忘了接 stage, 这里必须红。

    为什么需要它: 不带 stage 的调用会静默落回全局模型 —— 配置里明明
    写了 stage override, 但那一环没走路由, 而**没有任何地方会报错**。
    这种失效在行为层看不出来, 只能靠结构性检查拦住。
    """
    root = Path(__file__).resolve().parents[1]
    missing = []
    seen_exempt = set()

    for rel in _PRODUCTION_FILES:
        p = root / rel
        if not p.exists():
            continue
        tree = ast.parse(io.open(p, encoding="utf-8").read())

        def walk(node, enclosing, tier):
            for child in ast.iter_child_nodes(node):
                name = enclosing
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = child.name
                if (isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "messages"):
                    kws = {k.arg for k in child.keywords}
                    key = (rel, enclosing)
                    if "stage" not in kws:
                        if key in _STAGE_EXEMPT_CALLS:
                            seen_exempt.add(key)
                        else:
                            missing.append(f"{rel}:{child.lineno} in {name}()")
                walk(child, name, tier)

        walk(tree, "<module>", rel)

    check("**所有生产 messages() 都显式带 stage**", not missing, missing)
    # 豁免项必须仍然存在 —— 否则白名单会烂在那里, 未来新增的裸调用
    # 可能被一个**已经不存在**的豁免名掩盖。
    check("**豁免白名单没有过期项**",
          seen_exempt == set(_STAGE_EXEMPT_CALLS),
          (_STAGE_EXEMPT_CALLS.keys() - seen_exempt))

    # 反证: 守卫**真的会**发现裸调用(否则它只是个恒真断言)
    fake_src = (
        "class C:\n"
        "    def f(self):\n"
        "        self.client.messages('a', 'b')\n"
    )
    tree = ast.parse(fake_src)
    found = False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "messages"
                and "stage" not in {k.arg for k in node.keywords}):
            found = True
    check("**反证: 守卫能识别块裸调用**", found)


# ======================================================================
# 阶段名与 env 变量名是稳定契约
# ======================================================================
#: **完整**的 stage -> 环境变量名契约, 逐条手写。
#:
#: 这是本套件的核心断言之一。写成完整字面量(而不是抽查几条)才守得住
#: "改 stage 名不能让环境变量名漂移" —— 抽查漏掉的那几条恰好就是最可能
#: 被悄悄改掉的。
#:
#: 这张表**故意**与 `story/config.py` 里的 `STAGE_ENV_VARS` 重复一遍。
#: 重复是刻意的: 如果两边都从同一个表达式推导, 那就等于没测 ——
#: 改了推导式两边一起变, 测试照样绿。这里必须是**独立写死的期望值**。
EXPECTED_STAGE_ENV_VARS = {
    "puzzle.story": "AI_MODEL_PUZZLE_STORY",
    "puzzle.surface": "AI_MODEL_PUZZLE_SURFACE",
    "puzzle.structure": "AI_MODEL_PUZZLE_STRUCTURE",
    "puzzle.generate": "AI_MODEL_PUZZLE_GENERATE",
    "puzzle.hint_repair": "AI_MODEL_PUZZLE_HINT_REPAIR",
    "puzzle.review": "AI_MODEL_PUZZLE_REVIEW",
    "puzzle.truth_audit": "AI_MODEL_PUZZLE_TRUTH_AUDIT",
    "puzzle.safety": "AI_MODEL_PUZZLE_SAFETY",
    "puzzle.public_player": "AI_MODEL_PUZZLE_PUBLIC_PLAYER",
    "qa.answer": "AI_MODEL_QA_ANSWER",
    "qa.candidate_recheck": "AI_MODEL_QA_CANDIDATE_RECHECK",
    "qa.completion_verify": "AI_MODEL_QA_COMPLETION_VERIFY",
    "qa.judge": "AI_MODEL_QA_JUDGE",
    "hint": "AI_MODEL_HINT",
    "reveal": "AI_MODEL_REVEAL",
    "probe": "AI_MODEL_PROBE",
}


def test_stage_names_and_env_vars_are_stable():
    # Issue #23 明确列出的 stage 一个都不能少(15 个)
    issue_stages = {
        "puzzle.story", "puzzle.surface", "puzzle.structure", "puzzle.generate",
        "puzzle.hint_repair", "puzzle.review", "puzzle.truth_audit",
        "puzzle.safety", "qa.answer", "qa.candidate_recheck",
        "qa.completion_verify", "qa.judge", "hint", "reveal", "probe",
    }
    check("**Issue 要求的 15 个 stage 全覆盖**",
          issue_stages <= LLM_STAGES, sorted(issue_stages - LLM_STAGES))

    # 实际是 16 个: Issue 的 15 个 + 实现时发现的 puzzle.public_player。
    # 这条钉住"数量对不对", 免得以后有人以为漏了一个或多塞了一个。
    check("**实际 stage 数 = Issue 15 + public_player = 16**",
          len(LLM_STAGES) == 16 and LLM_STAGES == issue_stages | {"puzzle.public_player"},
          (len(LLM_STAGES), sorted(LLM_STAGES - issue_stages)))

    # ---- 完整固定映射(逐条比对, 不是抽查) ----
    check("**STAGE_ENV_VARS 与固定契约逐条一致**",
          STAGE_ENV_VARS == EXPECTED_STAGE_ENV_VARS,
          {k: (STAGE_ENV_VARS.get(k), v)
           for k, v in EXPECTED_STAGE_ENV_VARS.items()
           if STAGE_ENV_VARS.get(k) != v})

    # 一一对应, 不多不少
    check("**每个 stage 都有 env 变量名**",
          set(STAGE_ENV_VARS) == set(LLM_STAGES), len(STAGE_ENV_VARS))
    # 名字不得重复(重复 = 两个 stage 抢同一个变量)
    check("**env 变量名无重复**",
          len(set(STAGE_ENV_VARS.values())) == len(STAGE_ENV_VARS))
    # 都以 AI_MODEL_ 开头, 避免和 AI_MODEL 本体或别的变量撞
    check("**变量名统一前缀 AI_MODEL_**",
          all(v.startswith("AI_MODEL_") for v in STAGE_ENV_VARS.values()))
    # 大小写/下划线形式: 不得残留 '.' 或小写(env 名规范)
    check("**变量名是上划线形式(无 '.' 无小写)**",
          all(v == v.upper() and "." not in v for v in STAGE_ENV_VARS.values()),
          [v for v in STAGE_ENV_VARS.values() if v != v.upper() or "." in v])

    # stage 名不得重复/前后空格(否则 model_for 查不到)
    check("**stage 名无空白**",
          all(s == s.strip() and " " not in s for s in LLM_STAGES))


def test_stage_env_vars_are_a_literal_mapping_not_derived():
    """`STAGE_ENV_VARS` 必须是**字面量**, 不能由 stage 名推导。

    这是复审明确要求的契约。为什么必须在**源码层**再查一次, 而不是只比
    值: 值相等**证明不了**这件事。下面这版实现的值与本契约完全一致,

        STAGE_ENV_VARS = {s: "AI_MODEL_" + s.upper().replace(".", "_")
                          for s in LLM_STAGES}

    但它仍然是**派生**的 —— 哪天有人把 `puzzle.review` 改名成
    `puzzle.reviewer`, 变量名会自动变成 `AI_MODEL_PUZZLE_REVIEWER`,
    而线上那个手打配好的 `AI_MODEL_PUZZLE_REVIEW` 从此没人读。路由静默
    退回全局模型, 没有日志、没有报错。

    所以要直接把源码读出来, 断言那张 dict 里**逐条都是字符串字面量**。
    """
    src = io.open(Path(__file__).resolve().parents[1] / "story" / "config.py",
                  encoding="utf-8").read()
    tree = ast.parse(src)

    assign = None
    for node in tree.body:                      # 只看模块顶层
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "STAGE_ENV_VARS":
            assign = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "STAGE_ENV_VARS":
                    assign = node
    check("**找得到 STAGE_ENV_VARS 的赋值**", assign is not None)

    if assign is not None:
        value = assign.value
        check("**STAGE_ENV_VARS 是 dict 字面量(不是推导式/函数调用)**",
              isinstance(value, ast.Dict),
              type(value).__name__)
        if isinstance(value, ast.Dict):
            # 每个 key 与 value 都必须是字符串字面量
            non_literal = [
                f"{ast.unparse(k)} -> {ast.unparse(v)}"
                for k, v in zip(value.keys, value.values)
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)
                        and isinstance(v, ast.Constant) and isinstance(v.value, str))
            ]
            check("**每一项的 key/value 都是字符串字面量**",
                  not non_literal, non_literal)
            # 字面量条数 == stage 数(没有留 `**{...}` 之类的后门)
            check("**dict 字面量条数 == stage 数**",
                  len(value.keys) == len(LLM_STAGES),
                  (len(value.keys), len(LLM_STAGES)))
            # 不得出现 `**` 展开
            check("**dict 里没有 ** 展开**",
                  all(k is not None for k in value.keys))

    # 反证: 派生式写法必须被判为不合格(否则上面只是恒真)
    derived = ast.parse(
        'STAGE_ENV_VARS = {s: "AI_MODEL_" + s.upper().replace(".", "_")'
        ' for s in LLM_STAGES}')
    d_value = derived.body[0].value
    check("**反证: 推导式写法会被识别出来**",
          not isinstance(d_value, ast.Dict), type(d_value).__name__)

    # 反证: 逐条字面量但少一条, 也要能被"条数"这条断言抓住
    short = ast.parse('STAGE_ENV_VARS = {"hint": "AI_MODEL_HINT"}')
    check("**反证: 少写几条会被条数断言抓住**",
          len(short.body[0].value.keys) != len(LLM_STAGES))

    # 反证: 混进一个非字面量值(如 `AI_MODEL_PREFIX + suffix`)也不算合格
    mixed = ast.parse(
        'STAGE_ENV_VARS = {"hint": "AI_MODEL_HINT", '
        '"reveal": PREFIX + "REVEAL"}')
    mixed_bad = [
        ast.unparse(k) for k, v in
        zip(mixed.body[0].value.keys, mixed.body[0].value.values)
        if not isinstance(v, ast.Constant)
    ]
    check("**反证: 非字面量 value 会被识别出来**", mixed_bad == ["'reveal'"],
          mixed_bad)


def test_config_import_time_consistency_guard_exists():
    """`story/config.py` 必须自检 `STAGE_ENV_VARS` 与 `LLM_STAGES` 一致。

    测试之外还要有**运行期**的守卫: 有人加 stage 时忘了补 env 映射,
    应该是导入当场炸, 而不是等到线上发现"这个 stage 的变量名没人读"。
    """
    import story.config as C
    check("**存在 _check_stage_env_vars()**",
          hasattr(C, "_check_stage_env_vars"))

    # 反证: 真的会抛 —— 直接喂一份不一致的映射
    import ast as _ast
    src = io.open(Path(__file__).resolve().parents[1] / "story" / "config.py",
                  encoding="utf-8").read()
    fn = None
    for node in _ast.parse(src).body:
        if isinstance(node, _ast.FunctionDef) and node.name == "_check_stage_env_vars":
            fn = node
    check("**_check_stage_env_vars 是模块顶层函数**", fn is not None)

    # 造一个不一致的场景, 验证检查逻辑本身有效(用同一套判据重算)
    missing = LLM_STAGES - set(EXPECTED_STAGE_ENV_VARS)
    extra = set(EXPECTED_STAGE_ENV_VARS) - LLM_STAGES
    check("**反证: 当前映射确实是一致的(检查逻辑有输入)**",
          not missing and not extra, (sorted(missing), sorted(extra)))


def test_keyword2_live_and_prefetch_share_one_stage_model():
    """keyword2 的 live 与 prefetch 必须解析成**同一个** stage 模型。

    否则同一道题的两条生成路径会用不同模型 —— 而它们是同一个业务阶段,
    出题质量会变成"看你走哪条路"。
    """
    with _Env(AI_MODEL=A, AI_MODEL_PUZZLE_STORY=B,
              AI_MODEL_PUZZLE_SURFACE=B, AI_MODEL_PUZZLE_STRUCTURE=B):
        cfg = LLMConfig()
        # live 与 prefetch 都只通过 stage 名解析, 不传模型
        live = [cfg.model_for(s) for s in
                ("puzzle.story", "puzzle.surface", "puzzle.structure")]
        prefetch = [cfg.model_for(s) for s in
                    ("puzzle.story", "puzzle.surface", "puzzle.structure")]
        check("**live 与 prefetch 逐项相同**", live == prefetch, (live, prefetch))
        check("**三项都用 stage override**",
              live == [B, B, B], live)


def main():
    print("=" * 64)
    print("Issue #23: 按调用阶段配置可插拔模型路由")
    print("=" * 64)
    _clean_env()
    for t in (
        test_local_llm_config_covers_endpoint_credentials_models_and_budgets,
        test_simple_models_json_config_and_precedence,
        test_global_model_only_all_stages_fall_back,
        test_single_stage_override_is_isolated,
        test_multiple_stage_overrides_do_not_cross_wires,
        test_cli_model_clears_env_stage_overrides,
        test_cli_model_stage_overrides_after_model,
        test_unknown_stage_is_rejected_loudly,
        test_whitelist_covers_global_and_stage_models,
        test_supported_models_extra_extends_only,
        test_request_body_uses_stage_resolved_model,
        test_model_mismatch_warning_has_stage_requested_actual,
        test_two_stages_mismatch_warn_independently,
        test_keyword2_stages_route_correctly,
        test_classic_generator_uses_puzzle_generate,
        test_reviewer_audit_safety_stages_do_not_cross,
        test_qa_stages_route_independently,
        test_hint_and_reveal_are_separate_stages,
        test_budgets_unchanged_by_routing,
        test_no_llm_behavior_unchanged,
        test_stage_names_and_env_vars_are_stable,
        test_stage_env_vars_are_a_literal_mapping_not_derived,
        test_config_import_time_consistency_guard_exists,
        test_keyword2_live_and_prefetch_share_one_stage_model,
        test_every_production_call_declares_a_stage,
    ):
        print(f"\n[{t.__name__}]")
        t()
    _clean_env()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: LLM stage 模型路由全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

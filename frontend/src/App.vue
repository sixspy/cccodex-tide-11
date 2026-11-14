<script setup>
import { computed, onMounted, ref } from "vue";

const token = ref(localStorage.getItem("tide.token") || "");
const profile = ref(JSON.parse(localStorage.getItem("tide.profile") || "null"));
const username = ref("admin");
const password = ref("123456");
const loginError = ref("");
const batches = ref([]);
const current = ref(null);
const title = ref("");
const rawText = ref("");
const formError = ref("");
const busy = ref(false);

const view = ref("desk"); // desk | contests | yielded | setting
const yieldState = ref({ methods: [], setting: null, write_in_progress: false });
const chosenMethod = ref("measured_keep");
const settingMsg = ref("");
const settingError = ref("");
const contests = ref([]);
const contestDetail = ref(null);
const yielded = ref([]);
const detailBusy = ref(false);

const canWrite = computed(() => profile.value && profile.value.role === "writer");

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token.value) headers.Authorization = `Bearer ${token.value}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data.detail || "请求没有完成";
    throw new Error(typeof message === "string" ? message : "请求没有完成");
  }
  return data;
}

async function refresh() {
  batches.value = await request("/api/batches");
  if (current.value) {
    await openBatch(current.value.batch.id);
  } else if (batches.value.length) {
    await openBatch(batches.value[0].id);
  }
}

async function openBatch(id) {
  current.value = await request(`/api/batches/${id}`);
}

async function submitLogin() {
  loginError.value = "";
  try {
    const data = await request("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username.value, password: password.value }),
    });
    token.value = data.access_token;
    profile.value = { username: data.username, role: data.role, title: data.title };
    localStorage.setItem("tide.token", token.value);
    localStorage.setItem("tide.profile", JSON.stringify(profile.value));
    await refresh();
  } catch (error) {
    loginError.value = error.message;
  }
}

async function submitBatch() {
  formError.value = "";
  busy.value = true;
  try {
    const payload = { title: title.value, raw_text: rawText.value };
    if (canWrite.value) payload.yield_method = chosenMethod.value;
    const data = await request("/api/batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    title.value = "";
    rawText.value = "";
    await refresh();
    current.value = data;
    view.value = "desk";
  } catch (error) {
    formError.value = error.message;
  } finally {
    busy.value = false;
  }
}

async function loadSetting() {
  settingMsg.value = "";
  settingError.value = "";
  try {
    yieldState.value = await request("/api/yield-setting");
    if (yieldState.value.setting) {
      chosenMethod.value = yieldState.value.setting.yield_method;
    }
  } catch (error) {
    settingError.value = error.message;
  }
}

async function saveSetting() {
  settingMsg.value = "";
  settingError.value = "";
  try {
    const data = await request("/api/yield-setting", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ yield_method: chosenMethod.value }),
    });
    settingMsg.value = `已封存：${data.method_label}（${data.sealed_by} 于 ${data.sealed_at}），当天不得更换`;
    await loadSetting();
  } catch (error) {
    settingError.value = error.message;
  }
}

async function loadContests() {
  contests.value = await request("/api/flag-contests");
  contestDetail.value = null;
}

async function openContest(id) {
  detailBusy.value = true;
  try {
    contestDetail.value = await request(`/api/flag-contests/${id}`);
  } finally {
    detailBusy.value = false;
  }
}

async function loadYielded() {
  yielded.value = await request("/api/yielded-queue");
}

async function switchView(next) {
  view.value = next;
  if (next === "contests") await loadContests();
  else if (next === "yielded") await loadYielded();
  else if (next === "setting") await loadSetting();
}

function logout() {
  token.value = "";
  profile.value = null;
  current.value = null;
  localStorage.removeItem("tide.token");
  localStorage.removeItem("tide.profile");
}

onMounted(async () => {
  if (token.value) {
    try {
      await refresh();
      await loadSetting();
    } catch {
      logout();
    }
  }
});
</script>

<template>
  <header class="top">
    <div>
      <p class="eyebrow">水文组 · 报文归集</p>
      <h1>潮位报文归集台</h1>
    </div>
    <div v-if="profile" class="who">
      <nav class="tabs">
        <button :class="{ on: view === 'desk' }" @click="view = 'desk'">归集台</button>
        <button :class="{ on: view === 'contests' }" @click="switchView('contests')">旗争列表</button>
        <button :class="{ on: view === 'yielded' }" @click="switchView('yielded')">落选队列</button>
        <button :class="{ on: view === 'setting' }" @click="switchView('setting')">让位设置</button>
      </nav>
      <span>{{ profile.title }} {{ profile.username }}</span>
      <button class="ghost" @click="logout">离开</button>
    </div>
  </header>

  <main v-if="!profile" class="gate">
    <form @submit.prevent="submitLogin">
      <p>站码、时刻、潮位厘米、质量旗。一行一条，竖线分开。</p>
      <label>用户<input v-model="username" autocomplete="username" /></label>
      <label>口令<input v-model="password" type="password" autocomplete="current-password" /></label>
      <button>进入归集台</button>
      <p v-if="loginError" class="warn">{{ loginError }}</p>
    </form>
  </main>

  <main v-else class="desk">
    <!-- 归集台 -->
    <template v-if="view === 'desk'">
      <section class="paste">
        <h2>贴入这一批</h2>
        <p class="rule">
          质量旗 A 实测，M 插补，B 可疑。同一站同一时刻实测旗与插补旗同时到来，按当天让位方式裁决，只放行一种结局。
        </p>
        <label>批次名<input v-model="title" :disabled="!canWrite" placeholder="例如 堡镇 11-13 午班" /></label>
        <label>
          本批让位方式（与当天封存不一致会写「让位方式不符」）
          <select v-model="chosenMethod" :disabled="!canWrite">
            <option v-for="m in yieldState.methods" :key="m.value" :value="m.value">{{ m.label }}</option>
          </select>
        </label>
        <div class="seal-line">
          <template v-if="yieldState.setting">
            <span class="tag sealed">当天已封存：{{ yieldState.setting.method_label }}</span>
          </template>
          <template v-else>
            <span class="tag open">当天尚未封存，旗争行会挡下</span>
          </template>
          <span v-if="yieldState.write_in_progress" class="tag busy">让位写入中，设置锁定</span>
        </div>
        <textarea
          v-model="rawText"
          :disabled="!canWrite"
          placeholder="BZ03|202611130900|176|A"
          spellcheck="false"
        />
        <button :disabled="!canWrite || busy" @click="submitBatch">归集这一批</button>
        <p v-if="!canWrite" class="quiet">档案员只能翻已落入的报文。</p>
        <p v-if="formError" class="warn">{{ formError }}</p>
      </section>

      <section class="slips">
        <h2>已收批次</h2>
        <button
          v-for="batch in batches"
          :key="batch.id"
          class="slip"
          :class="{ on: current && current.batch.id === batch.id }"
          @click="openBatch(batch.id)"
        >
          <strong>{{ batch.title }}</strong>
          <em>{{ batch.status }}</em>
          <span>
            落入 {{ batch.accepted_count }} 行 · 未接纳 {{ batch.rejected_count }} 行
            <template v-if="batch.yielded_count"> · 让位 {{ batch.yielded_count }} 行</template>
          </span>
        </button>

        <article v-if="current" class="tape">
          <header>
            <h3>{{ current.batch.title }}</h3>
            <span>{{ current.batch.status }}</span>
          </header>
          <h4>已落库</h4>
          <table>
            <thead>
              <tr><th>行</th><th>站</th><th>时刻</th><th>厘米</th><th>旗</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in current.landed" :key="row.line_no">
                <td>{{ row.line_no }}</td>
                <td>{{ row.station_code }}</td>
                <td>{{ row.observed_at }}</td>
                <td>{{ row.tide_cm }}</td>
                <td>{{ row.flag_label }}</td>
              </tr>
              <tr v-if="!current.landed.length"><td colspan="5">这一批没有落入的行</td></tr>
            </tbody>
          </table>
          <h4>旗争让位（落选）</h4>
          <ul v-if="current.yielded.length">
            <li v-for="row in current.yielded" :key="row.line_no">
              <code>{{ row.station_code }} | {{ row.observed_at }} | {{ row.tide_cm }}cm | {{ row.flag_label }}</code>
              <span>{{ row.reason }}</span>
            </li>
          </ul>
          <p v-else class="quiet">这一批没有旗争让位的行。</p>
          <h4>未接纳</h4>
          <ul>
            <li v-for="row in current.rejected" :key="row.line_no">
              <code>{{ row.raw_line }}</code>
              <span>{{ row.reason }}</span>
            </li>
            <li v-if="!current.rejected.length">没有被挡下的行</li>
          </ul>
        </article>
      </section>
    </template>

    <!-- 旗争列表与详情 -->
    <section v-else-if="view === 'contests'" class="panel">
      <h2>旗争列表</h2>
      <p class="rule">同一站同一时刻实测旗与插补旗对撞，每场只留一种结局。</p>
      <table v-if="contests.length" class="contest-table">
        <thead>
          <tr>
            <th>站 / 时刻</th><th>留下的旗</th><th>落选</th><th>让位次数</th><th>方式名</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in contests" :key="c.id" :class="{ picked: contestDetail && contestDetail.contest.id === c.id }">
            <td>
              <strong>{{ c.station_code }}</strong>
              <span class="dim">{{ c.observed_at }}</span>
            </td>
            <td>
              <span v-if="c.winner_flag" class="tag winner">{{ c.winner_flag_label }} · {{ c.winner_tide_cm }}cm</span>
              <span v-else class="tag none">无旗留库</span>
            </td>
            <td>{{ c.loser_count }} 旗</td>
            <td>{{ c.yield_count }}</td>
            <td>{{ c.method_label }}</td>
            <td><button class="mini" @click="openContest(c.id)">对照详情</button></td>
          </tr>
        </tbody>
      </table>
      <p v-else class="quiet">还没有旗争记录。</p>

      <article v-if="detailBusy" class="tape">在翻这场旗争……</article>
      <article v-else-if="contestDetail" class="tape detail">
        <header>
          <h3>{{ contestDetail.contest.station_code }} · {{ contestDetail.contest.observed_at }}</h3>
          <span>{{ contestDetail.contest.method_label }}</span>
        </header>
        <p class="rule">
          结局：
          <span v-if="contestDetail.contest.winner_flag" class="tag winner">
            留下 {{ contestDetail.contest.winner_flag_label }}（{{ contestDetail.contest.winner_tide_cm }}cm，批次 {{ contestDetail.contest.winner_batch_id }} 第 {{ contestDetail.contest.winner_line_no }} 行）
          </span>
          <span v-else class="tag none">两行都不入库</span>
          ，让位次数 {{ contestDetail.contest.yield_count }}。
        </p>
        <h4>两行厘米对照</h4>
        <table>
          <thead>
            <tr><th>结局</th><th>旗</th><th>厘米</th><th>批次 / 行</th><th>原因</th></tr>
          </thead>
          <tbody>
            <tr v-if="contestDetail.contest.winner_flag" class="row-winner">
              <td><span class="tag winner">留下</span></td>
              <td>{{ contestDetail.contest.winner_flag_label }}</td>
              <td>{{ contestDetail.contest.winner_tide_cm }}</td>
              <td>批次 {{ contestDetail.contest.winner_batch_id }} · 第 {{ contestDetail.contest.winner_line_no }} 行</td>
              <td>实测留库</td>
            </tr>
            <tr v-for="loser in contestDetail.losers" :key="loser.id" class="row-loser">
              <td><span class="tag none">落选</span></td>
              <td>{{ loser.flag_label }}</td>
              <td>{{ loser.tide_cm }}</td>
              <td>批次 {{ loser.batch_id }} · 第 {{ loser.line_no }} 行</td>
              <td>{{ loser.reason }}</td>
            </tr>
          </tbody>
        </table>
      </article>
    </section>

    <!-- 落选队列 -->
    <section v-else-if="view === 'yielded'" class="panel">
      <h2>落选队列</h2>
      <p class="rule">旗争中没有入库的旗逐行留底。</p>
      <table v-if="yielded.length" class="contest-table">
        <thead>
          <tr><th>站 / 时刻</th><th>旗</th><th>厘米</th><th>原因</th><th>来源批次 / 行</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in yielded" :key="row.id">
            <td><strong>{{ row.station_code }}</strong><span class="dim">{{ row.observed_at }}</span></td>
            <td>{{ row.flag_label }}</td>
            <td>{{ row.tide_cm }}</td>
            <td>{{ row.reason }}</td>
            <td>{{ row.batch_title }} · 第 {{ row.line_no }} 行</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="quiet">落选队列是空的。</p>
    </section>

    <!-- 让位设置 -->
    <section v-else class="panel setting">
      <h2>让位设置</h2>
      <p class="rule">让位方式选定后即封存，当天不得更换；让位尚未写完时这里锁定，不许改。</p>
      <template v-if="yieldState.setting">
        <div class="seal-card">
          <span class="tag sealed">当天已封存</span>
          <p><strong>{{ yieldState.setting.method_label }}</strong></p>
          <p class="dim">封存人 {{ yieldState.setting.sealed_by }} · {{ yieldState.setting.sealed_at }}</p>
        </div>
      </template>
      <template v-else>
        <label>
          选择让位方式
          <select v-model="chosenMethod" :disabled="!canWrite || yieldState.write_in_progress">
            <option v-for="m in yieldState.methods" :key="m.value" :value="m.value">{{ m.label }}</option>
          </select>
        </label>
        <button :disabled="!canWrite || yieldState.write_in_progress" @click="saveSetting">封存当天让位方式</button>
      </template>
      <p v-if="yieldState.write_in_progress" class="warn">让位正在写入，设置暂时锁定，改不了。</p>
      <p v-if="!canWrite" class="quiet">档案员只能看，不能封存。</p>
      <p v-if="settingMsg" class="ok">{{ settingMsg }}</p>
      <p v-if="settingError" class="warn">{{ settingError }}</p>
    </section>
  </main>
</template>

<style scoped>
.top, .desk, .gate { width: min(1180px, calc(100% - 32px)); margin: 0 auto; }
.top { display: flex; justify-content: space-between; align-items: end; padding: 28px 0 12px; gap: 16px; flex-wrap: wrap; }
.eyebrow { letter-spacing: 0.18em; text-transform: uppercase; color: #d7a15f; font-size: 12px; margin: 0 0 6px; }
h1 { margin: 0; font-weight: 560; font-size: 34px; }
h2, h3, h4 { margin: 0; }
.who { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.tabs { display: flex; gap: 6px; }
.tabs button { background: transparent; color: #f3efe2; border: 1px solid rgba(243, 239, 226, 0.25); padding: 6px 12px; font-size: 13px; }
.tabs button.on { border-color: #d7a15f; color: #d7a15f; }
.ghost { background: transparent; color: #f3efe2; border: 1px solid rgba(243, 239, 226, 0.35); padding: 6px 12px; }
.gate { padding: 24px 0 48px; }
.gate form, .paste, .tape, .panel, .seal-card {
  background: #f3efe2;
  color: #1c2430;
  padding: 22px;
}
.gate form { width: min(420px, 100%); display: grid; gap: 12px; }
label { display: grid; gap: 6px; font-size: 14px; }
input, textarea, select {
  width: 100%;
  border: 0;
  background: #fffdf8;
  color: #1c2430;
  padding: 10px 12px;
  box-shadow: inset 0 0 0 1px #d9d0bc;
}
textarea { min-height: 220px; font-family: "IBM Plex Mono", Consolas, monospace; line-height: 1.6; }
button { background: #9d2c2c; color: #fffaf2; padding: 10px 14px; }
.mini { padding: 5px 10px; font-size: 12px; }
.desk { display: grid; grid-template-columns: 360px 1fr; gap: 22px; padding-bottom: 48px; }
.desk .panel { grid-column: 1 / -1; }
.paste, .slips { display: grid; gap: 12px; align-content: start; }
.rule, .quiet { margin: 0; color: #5c5648; font-size: 14px; }
.dim { color: #7a7363; font-size: 12px; display: block; }
.seal-line { display: flex; gap: 8px; flex-wrap: wrap; }
.tag { display: inline-block; padding: 3px 9px; font-size: 12px; border-radius: 2px; }
.tag.sealed { background: #e3d3b0; color: #5c4415; }
.tag.open { background: #dde4ee; color: #2c3e58; }
.tag.busy { background: #f3d2d2; color: #9d2c2c; }
.tag.winner { background: #dcead8; color: #2c5d33; }
.tag.none { background: #e6e0d2; color: #6b6353; }
.slip {
  text-align: left;
  background: transparent;
  color: #f3efe2;
  border: 1px solid rgba(243, 239, 226, 0.2);
  padding: 12px 14px;
  display: grid;
  gap: 4px;
}
.slip.on { border-color: #d7a15f; }
.slip em { font-style: normal; color: #d7a15f; }
.slip span, .tape li span { color: #5c5648; }
.tape { margin-top: 8px; display: grid; gap: 6px; }
.tape header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
.tape h4 { margin: 14px 0 8px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid #e4dcc8; font-size: 14px; vertical-align: top; }
.panel { display: grid; gap: 14px; align-content: start; }
.contest-table tr.picked { background: #f7eeda; }
.row-winner { background: #eef5ea; }
.row-loser { background: #f7f2e8; }
ul { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
li { display: grid; gap: 2px; }
code { font-family: Consolas, monospace; }
.warn { color: #9d2c2c; margin: 0; }
.ok { color: #2c5d33; margin: 0; }
.setting { max-width: 560px; gap: 14px; }
.seal-card { box-shadow: inset 0 0 0 1px #d9d0bc; display: grid; gap: 6px; }
.seal-card p { margin: 0; }
@media (max-width: 900px) {
  .desk { grid-template-columns: 1fr; }
}
</style>

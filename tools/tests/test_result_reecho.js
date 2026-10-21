// 复现并锁死「上次结论每开一次弹窗就多一行」的缺陷。
//
// 症状（用户 2026-10-09 贴图）：状态框里堆了 5 条
// 「（以上为上次操作的结论，发生于 N 分钟前。）」，且 N 前后不一致。
//
// 根因：showStatus() 会调 persistStatus() 把**屏幕上的文字**写回 storage，
// 而恢复分支写字时又拼了一遍注解 → 下一次打开读到的是「带注解的文本」，
// 再拼一遍 → 每开一次弹窗长一行（同时把 at 刷成现在，时间因此对不上）。
//
// 修复：注解只在渲染时拼（withAgeNote），回显一律 { persist: false }。
const fs = require("fs");
const path = require("path");
const EXT = path.join(__dirname, "..", "clip-extension");
const src = fs.readFileSync(path.join(EXT, "popup.js"), "utf8");

const m = src.match(/function withAgeNote\(msg, ageMin\) \{[\s\S]*?\n\}/);
const withAgeNote = m ? eval("(" + m[0].replace(/^function/, "function") + ")") : null;

// stripAgeNotes 依赖一个模块级常量 AGE_NOTE_RE，这里原样取出一起求值。
const reSrc = (src.match(/const AGE_NOTE_RE = .*;/) || [])[0] || "";
const fnSrc = (src.match(/function stripAgeNotes\(msg\) \{[\s\S]*?\n\}/) || [])[0] || "";
const stripAgeNotes = fnSrc
  ? new Function(reSrc.replace(/^const /, "var ") + "\nreturn (" + fnSrc + ");")()
  : null;

const countNotes = s => (s.match(/以上为上次操作的结论/g) || []).length;
let ok = 0, fail = 0;
function check(name, got, want) {
  const good = JSON.stringify(got) === JSON.stringify(want);
  if (good) ok++; else fail++;
  console.log((good ? "  PASS " : "  FAIL ") + name);
  if (!good) { console.log("         got : " + JSON.stringify(got)); console.log("         want: " + JSON.stringify(want)); }
}

const RAW = "📌 该链接已入库，无需再存入待审区\n已有文件：raw\\articles\\xxx-1.md";

console.log("=== 语义：注解不落盘 → 反复打开也不累积 ===");
check("抽出了 withAgeNote", typeof withAgeNote, "function");

// 修复后的模型：storage 始终存 raw，屏幕每次都只拼一次注解
let raw = RAW;
let lastShown = "";
for (let i = 0; i < 5; i++) {
  lastShown = withAgeNote(raw, 1);
  // storage 里仍是 raw（showStatus 带 persist:false 时不回写）
}
check("storage 里注解数为 0（裸结论）", countNotes(raw), 0);
check("屏幕上注解数为 1（不会堆叠）", countNotes(lastShown), 1);
check("屏幕文本 = 裸结论 + 一行注解",
  lastShown === RAW + "\n\n（以上为上次操作的结论，发生于 1 分钟前。）", true);

console.log();
console.log("=== 反例：修复前的写法会自增（证明这条断言抓得住 bug） ===");
let bad = RAW;
for (let i = 0; i < 5; i++) bad = withAgeNote(bad, 1);
check("旧写法 5 次后确有 5 行（说明这是真会发生的缺陷）", countNotes(bad), 5);

console.log();
console.log("=== 自愈：把历史遗留的多行注解剥回裸结论 ===");
check("抽出了 stripAgeNotes", typeof stripAgeNotes, "function");
check("  5 行注解 → 剥回裸结论", stripAgeNotes(bad), RAW);
check("  单行注解 → 同样剥净",
  stripAgeNotes(withAgeNote(RAW, 46)), RAW);
check("  裸结论 → 原样返回（不误伤正常内容）",
  stripAgeNotes(RAW), RAW);
check("  内容里含「以上为上次操作的结论」但非注解格式 → 不动它",
  stripAgeNotes("结论：以上为上次操作的结论是错的"), "结论：以上为上次操作的结论是错的");
check("  剥净后再渲染只得到 1 行注解（用户看到的就是干净的）",
  countNotes(withAgeNote(stripAgeNotes(bad), 1)), 1);

console.log();
console.log("=== 源码断言 ===");
check("恢复分支用 withAgeNote 且 persist:false",
  /showStatus\(last\.type, withAgeNote\(clean, ageMin\), \{ persist: false \}\)/.test(src), true);
check("恢复分支不再自己拼注解字符串",
  !/showStatus\(last\.type, last\.msg \+/.test(src) &&
  !/showStatus\(last\.type, last\.msg\s*\n/.test(src), true);
check("showStatus 支持 opts.persist === false（跳过落盘）",
  /function showStatus\(type, msg, opts\)/.test(src) &&
  /if \(!opts \|\| opts\.persist !== false\) \{\s*\n\s*persistStatus\(type, msg\);/.test(src), true);
check("waitForBackgroundResult 回显也不落盘",
  /showStatus\(cur\.type, cur\.msg, \{ persist: false \}\)/.test(src), true);
check("恢复分支会自愈历史遗留的多行注解",
  /const clean = stripAgeNotes\(last\.msg\)/.test(src) &&
  /if \(clean !== last\.msg\)/.test(src) &&
  /msg: clean/.test(src), true);
// 只数「会拼进用户看到的文本」的地方：withAgeNote 的模板串 1 处，
// 另加 stripAgeNotes 用的匹配正则 1 处 —— 两处都必须存在且语义相反。
const codePieces = src.split("\n")
  .filter(l => !/^\s*(\/\/|\*|\/\*)/.test(l))
  .join("\n")
  .match(/以上为上次操作的结论/g) || [];
check("拼接注解只在 withAgeNote 与 strip 正则两处（注释不算）", codePieces.length, 2);
check("background 写回的是干净结论（不含注解）",
  !/以上为上次操作的结论/.test(fs.readFileSync(path.join(EXT, "background.js"), "utf8")), true);

console.log();
console.log("结果: " + ok + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);

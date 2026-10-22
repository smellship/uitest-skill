// 索引标记的最外层清点：跨帧使用，只碰标记，不产生任何元素数据。
// 由 Python 侧逐帧 evaluate 调用，args = {op, mark}。
// op=clear 把这些标记全部抹掉并返回抹掉的数量；op=count 只数不清。
// 为什么要单独一个脚本：snapshot.js 只会清"本次求值的这一帧自己的 root"，
// 而微前端（如 wujie）会让两个 frame 看到同一批节点，各自清各自的会把对方刚打的标记抹掉。
(args) => {
  const MARK = args.mark;
  const roots = () => {
    const out = [document];
    for (let i = 0; i < out.length; i++) {
      for (const e of out[i].querySelectorAll('*')) if (e.shadowRoot) out.push(e.shadowRoot);
    }
    return out;
  };
  let seen = 0;
  for (const root of roots()) {
    const hits = root.querySelectorAll('[' + MARK + ']');
    seen += hits.length;
    if (args.op === 'clear') hits.forEach(e => e.removeAttribute(MARK));
  }
  return seen;
}

try {
  const r = await fetch('http://127.0.0.1:3000/__reset',{method:'POST'});
  if (!r.ok) {
    console.error(`服务端状态重置失败（HTTP ${r.status}）`);
    process.exit(1);
  }
  console.log('服务端状态已重置');
} catch (error) {
  console.error('服务端状态重置失败：', error);
  process.exitCode = 1;
}

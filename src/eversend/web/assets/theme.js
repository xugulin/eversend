/* 主题预加载：在样式生效前把 html[data-theme] 定好，避免先闪一下深色再变浅色。
 *
 * 为什么是一个单独的文件而不是 <head> 里的一段内联脚本：页面的 CSP 是
 * `script-src 'self'`，内联脚本会被浏览器直接拒掉（控制台里一条报错，主题也
 * 记不住）。这个文件很小，又在局域网里，代价可以忽略。
 *
 * app.js 里的 bindTheme() 还会再读一次 localStorage —— 两边都以这里为准。
 *
 * 这个属性是**唯一**的开关：颜色本身是 app.css 顶部那一组 CSS 变量
 * （--canvas / --surface / --raised / --overlay / --border / --text / --accent /
 * --success / --warning / --danger / --info，值与 desktop/theme.py 逐字一致），
 * 这里只负责在样式生效前把 html[data-theme="light"] 定好，别的什么都不改。
 */
(function () {
  'use strict';
  try {
    if (localStorage.getItem('eversend-theme') === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
      var meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.setAttribute('content', '#ffffff');
    }
  } catch (error) {
    /* 无痕模式/禁 localStorage：默认深色就好 */
  }
})();

/**
 * YRC SYS — NEXT-GEN 3D ENGINE & CINEMATIC MICRO-INTERACTIONS
 * Holographic Laser Top Progress Bar, Tactile Button Feedback,
 * Three.js 3D Holographic Geometry, Dynamic 3D Card Tilt, Particle WebGL,
 * Live Telemetry Clock, Magnetic Interactions & Animated Number Counters.
 */

(function () {
  'use strict';

  // Check user motion preference
  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ==========================================================================
     1. HOLOGRAPHIC TOP LASER PROGRESS BAR & NON-BLOCKING LOADING EXPERIENCE
     ========================================================================== */
  function initHoloProgressBar() {
    const bar = document.getElementById('holoProgressBar');
    if (!bar) return;

    let progressTimer = null;

    function start() {
      const pageLoader = document.getElementById('pageLoader');
      if (pageLoader && pageLoader.getAttribute('aria-hidden') === 'false') return;

      if (progressTimer) clearInterval(progressTimer);
      bar.classList.remove('done');
      bar.classList.add('active');
      bar.style.width = '20%';

      let current = 20;
      progressTimer = setInterval(() => {
        if (current < 80) {
          current += Math.random() * 12 + 4;
          bar.style.width = Math.min(current, 85) + '%';
        } else {
          clearInterval(progressTimer);
        }
      }, 150);
    }

    function finish() {
      if (progressTimer) clearInterval(progressTimer);
      bar.style.width = '100%';
      bar.classList.add('done');
      setTimeout(() => {
        bar.classList.remove('active', 'done');
        bar.style.width = '0%';
      }, 400);

      // Restore any loading buttons
      document.querySelectorAll('.btn.is-loading').forEach((btn) => {
        btn.classList.remove('is-loading');
        if (btn.hasAttribute('data-original-html')) {
          btn.innerHTML = btn.getAttribute('data-original-html');
        }
      });
    }

    // Intercept internal page navigations
    document.addEventListener('click', (e) => {
      const link = e.target.closest('a[href]');
      if (!link || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || link.target === '_blank' || link.hasAttribute('download')) return;
      const u = new URL(link.href, location.href);
      if (u.origin !== location.origin || (u.pathname === location.pathname && u.search === location.search) || u.pathname.endsWith('.csv') || (link.hash && u.pathname === location.pathname)) return;
      start();
    });

    // Intercept form submissions with tactile button feedback
    document.addEventListener('submit', (e) => {
      if (e.defaultPrevented) return;
      start();

      const submitBtn = e.target.querySelector('button[type=submit], button:not([type])');
      if (submitBtn && !submitBtn.classList.contains('is-loading')) {
        submitBtn.classList.add('is-loading');
        submitBtn.setAttribute('data-original-html', submitBtn.innerHTML);

        // Custom copy for login vs general forms
        if (e.target.classList.contains('login-form')) {
          submitBtn.innerHTML = '<span class="btn-spinner"></span> Authenticating session...';
        } else {
          submitBtn.innerHTML = '<span class="btn-spinner"></span> Processing...';
        }
      }
    });

    window.addEventListener('pageshow', finish);
    window.addEventListener('load', finish);
  }

  /* ==========================================================================
     2. AMBIENT 3D PARTICLE CONSTELLATION CANVAS (INTERNAL PAGES)
     ========================================================================== */
  function initAmbientParticles() {
    const canvas = document.getElementById('ambientCanvas');
    if (!canvas || prefersReducedMotion) return;

    const ctx = canvas.getContext('2d');
    let width = (canvas.width = window.innerWidth);
    let height = (canvas.height = window.innerHeight);

    window.addEventListener('resize', () => {
      width = canvas.width = window.innerWidth;
      height = canvas.height = window.innerHeight;
    });

    const numParticles = Math.min(65, Math.floor((width * height) / 18000));
    const particles = [];
    const mouse = { x: -1000, y: -1000, targetX: -1000, targetY: -1000 };

    window.addEventListener('pointermove', (e) => {
      mouse.targetX = e.clientX;
      mouse.targetY = e.clientY;
    });

    for (let i = 0; i < numParticles; i++) {
      particles.push({
        x: Math.random() * width,
        y: Math.random() * height,
        vx: (Math.random() - 0.5) * 0.45,
        vy: (Math.random() - 0.5) * 0.45,
        radius: Math.random() * 1.8 + 0.6,
        baseAlpha: Math.random() * 0.35 + 0.15,
        alpha: 0.2,
        isCrimson: Math.random() < 0.25
      });
    }

    function render() {
      ctx.clearRect(0, 0, width, height);

      mouse.x += (mouse.targetX - mouse.x) * 0.08;
      mouse.y += (mouse.targetY - mouse.y) * 0.08;

      for (let i = 0; i < particles.length; i++) {
        const p = particles[i];
        p.x += p.vx;
        p.y += p.vy;

        if (p.x < 0) p.x = width;
        if (p.x > width) p.x = 0;
        if (p.y < 0) p.y = height;
        if (p.y > height) p.y = 0;

        // Mouse avoidance / gentle kinetic push
        const dx = p.x - mouse.x;
        const dy = p.y - mouse.y;
        const dist = Math.hypot(dx, dy);
        if (dist < 120 && dist > 0) {
          const force = (120 - dist) / 120;
          p.x += (dx / dist) * force * 1.5;
          p.y += (dy / dist) * force * 1.5;
        }

        // Draw particle
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
        ctx.fillStyle = p.isCrimson
          ? `rgba(255, 30, 66, ${p.baseAlpha * 1.4})`
          : `rgba(255, 255, 255, ${p.baseAlpha})`;
        ctx.fill();

        // Connect nearby particles
        for (let j = i + 1; j < particles.length; j++) {
          const p2 = particles[j];
          const dist2 = Math.hypot(p.x - p2.x, p.y - p2.y);
          if (dist2 < 110) {
            ctx.beginPath();
            ctx.moveTo(p.x, p.y);
            ctx.lineTo(p2.x, p2.y);
            const lineAlpha = (1 - dist2 / 110) * 0.12;
            ctx.strokeStyle = p.isCrimson || p2.isCrimson
              ? `rgba(227, 24, 55, ${lineAlpha * 1.5})`
              : `rgba(255, 255, 255, ${lineAlpha})`;
            ctx.lineWidth = 0.75;
            ctx.stroke();
          }
        }
      }

      requestAnimationFrame(render);
    }
    render();
  }

  /* ==========================================================================
     3. 3D HOLOGRAPHIC GEOMETRY & PARTICLES (LOGIN PAGE)
     ========================================================================== */
  function initLogin3DScene() {
    const canvas = document.getElementById('loginCanvas');
    if (!canvas || prefersReducedMotion) return;

    if (window.THREE) {
      initThreeScene(canvas);
    } else {
      initCanvas3DFallback(canvas);
    }
  }

  function initThreeScene(canvas) {
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, window.innerWidth / window.innerHeight, 0.1, 1000);
    camera.position.z = 32;

    const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
    renderer.setSize(window.innerWidth, window.innerHeight);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

    window.addEventListener('resize', () => {
      camera.aspect = window.innerWidth / window.innerHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(window.innerWidth, window.innerHeight);
    });

    // Holographic Core: Outer Wireframe Icosahedron
    const icoGeo = new THREE.IcosahedronGeometry(7.5, 1);
    const icoMat = new THREE.MeshBasicMaterial({
      color: 0xe31837,
      wireframe: true,
      transparent: true,
      opacity: 0.38
    });
    const icosahedron = new THREE.Mesh(icoGeo, icoMat);
    scene.add(icosahedron);

    // Inner Glowing Core: Octahedron
    const octGeo = new THREE.OctahedronGeometry(4, 0);
    const octMat = new THREE.MeshBasicMaterial({
      color: 0xff3355,
      wireframe: true,
      transparent: true,
      opacity: 0.65
    });
    const octahedron = new THREE.Mesh(octGeo, octMat);
    scene.add(octahedron);

    // Orbital Rings
    const ringGeo1 = new THREE.TorusGeometry(10, 0.05, 16, 100);
    const ringMat1 = new THREE.MeshBasicMaterial({ color: 0xff1e42, transparent: true, opacity: 0.45 });
    const ring1 = new THREE.Mesh(ringGeo1, ringMat1);
    ring1.rotation.x = Math.PI / 3;
    scene.add(ring1);

    const ringGeo2 = new THREE.TorusGeometry(12, 0.04, 16, 100);
    const ringMat2 = new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.25 });
    const ring2 = new THREE.Mesh(ringGeo2, ringMat2);
    ring2.rotation.y = Math.PI / 4;
    scene.add(ring2);

    // Floating Stardust Particles
    const particleCount = 450;
    const geometry = new THREE.BufferGeometry();
    const positions = new Float32Array(particleCount * 3);
    const colors = new Float32Array(particleCount * 3);

    const colorCrimson = new THREE.Color(0xff1e42);
    const colorWhite = new THREE.Color(0xffffff);
    const colorAmber = new THREE.Color(0xff9f1c);

    for (let i = 0; i < particleCount; i++) {
      positions[i * 3] = (Math.random() - 0.5) * 55;
      positions[i * 3 + 1] = (Math.random() - 0.5) * 55;
      positions[i * 3 + 2] = (Math.random() - 0.5) * 35;

      const rand = Math.random();
      const chosenColor = rand < 0.4 ? colorCrimson : rand < 0.8 ? colorWhite : colorAmber;
      colors[i * 3] = chosenColor.r;
      colors[i * 3 + 1] = chosenColor.g;
      colors[i * 3 + 2] = chosenColor.b;
    }

    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));

    const pMaterial = new THREE.PointsMaterial({
      size: 0.4,
      vertexColors: true,
      transparent: true,
      opacity: 0.65
    });
    const particles = new THREE.Points(geometry, pMaterial);
    scene.add(particles);

    // Mouse Tracking Parallax
    let mouseX = 0;
    let mouseY = 0;
    let targetX = 0;
    let targetY = 0;

    window.addEventListener('pointermove', (e) => {
      targetX = (e.clientX / window.innerWidth - 0.5) * 2;
      targetY = (e.clientY / window.innerHeight - 0.5) * 2;
    });

    function animate() {
      requestAnimationFrame(animate);

      mouseX += (targetX - mouseX) * 0.05;
      mouseY += (targetY - mouseY) * 0.05;

      icosahedron.rotation.x += 0.003;
      icosahedron.rotation.y += 0.004;

      octahedron.rotation.x -= 0.005;
      octahedron.rotation.y -= 0.006;

      ring1.rotation.z += 0.002;
      ring2.rotation.z -= 0.0025;

      camera.position.x = mouseX * 5;
      camera.position.y = -mouseY * 4;
      camera.lookAt(scene.position);

      renderer.render(scene, camera);
    }
    animate();
  }

  function initCanvas3DFallback(canvas) {
    const ctx = canvas.getContext('2d');
    let width = (canvas.width = window.innerWidth);
    let height = (canvas.height = window.innerHeight);

    window.addEventListener('resize', () => {
      width = canvas.width = window.innerWidth;
      height = canvas.height = window.innerHeight;
    });

    const numPoints = 120;
    const points = [];
    for (let i = 0; i < numPoints; i++) {
      points.push({
        x: (Math.random() - 0.5) * width,
        y: (Math.random() - 0.5) * height,
        z: Math.random() * 800 + 100,
        color: Math.random() < 0.35 ? '#ff1e42' : '#ffffff'
      });
    }

    let angleX = 0, angleY = 0;
    let targetAngleX = 0, targetAngleY = 0;

    window.addEventListener('pointermove', (e) => {
      targetAngleX = (e.clientY / height - 0.5) * 0.6;
      targetAngleY = (e.clientX / width - 0.5) * 0.6;
    });

    function draw() {
      ctx.clearRect(0, 0, width, height);

      angleX += (targetAngleX - angleX) * 0.05;
      angleY += (targetAngleY - angleY) * 0.05;

      ctx.save();
      ctx.translate(width / 2, height / 2);

      for (let i = 0; i < points.length; i++) {
        const p = points[i];
        p.z -= 0.8;
        if (p.z <= 0) p.z = 800;

        const fov = 400;
        const scale = fov / (fov + p.z);
        const x2d = p.x * scale;
        const y2d = p.y * scale;

        ctx.beginPath();
        ctx.arc(x2d, y2d, Math.max(0.5, 2 * scale), 0, Math.PI * 2);
        ctx.fillStyle = p.color;
        ctx.globalAlpha = Math.min(1, scale * 1.5);
        ctx.fill();
      }

      ctx.restore();
      requestAnimationFrame(draw);
    }
    draw();
  }

  /* ==========================================================================
     4. 3D CARD HOVER & DYNAMIC SPECULAR TILT ENGINE
     ========================================================================== */
  function init3DCardTilt() {
    if (prefersReducedMotion || !window.matchMedia('(hover: hover)').matches) return;

    const cards = document.querySelectorAll(
      '.stat-card, .panel, .semester-card, .overview-tile, .login-form, .admin-card'
    );

    cards.forEach((card) => {
      let isHovered = false;

      card.addEventListener('pointerenter', () => {
        isHovered = true;
        card.style.transition = 'transform 0.12s ease-out, border-color 0.2s, box-shadow 0.2s';
      });

      card.addEventListener('pointermove', (e) => {
        if (!isHovered) return;
        const rect = card.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;

        const centerX = rect.width / 2;
        const centerY = rect.height / 2;

        const rotateX = ((y - centerY) / centerY) * -5.5; // subtle tilt
        const rotateY = ((x - centerX) / centerX) * 5.5;

        card.style.transform = `perspective(1000px) rotateX(${rotateX.toFixed(2)}deg) rotateY(${rotateY.toFixed(2)}deg) translateY(-2px)`;
      });

      card.addEventListener('pointerleave', () => {
        isHovered = false;
        card.style.transition = 'transform 0.45s cubic-bezier(0.16, 1, 0.3, 1), border-color 0.25s, box-shadow 0.25s';
        card.style.transform = 'perspective(1000px) rotateX(0deg) rotateY(0deg) translateY(0px)';
      });
    });
  }

  /* ==========================================================================
     5. ANIMATED NUMBER COUNTERS (FROM ACTUAL DATA)
     ========================================================================== */
  function initAnimatedCounters() {
    if (prefersReducedMotion) return;

    const targets = document.querySelectorAll(
      '.stat-card > strong, .overview-tile strong, .equity-score strong, .count-pill'
    );

    targets.forEach((elem) => {
      const rawText = elem.textContent.trim();
      const match = rawText.match(/^([0-9]+)(\s*.*)?$/);
      if (!match) return;

      const finalValue = parseInt(match[1], 10);
      const suffix = match[2] || '';
      if (isNaN(finalValue) || finalValue === 0) return;

      const duration = 1100; // ms
      const startTime = performance.now();

      function updateCounter(currentTime) {
        const elapsed = currentTime - startTime;
        const progress = Math.min(elapsed / duration, 1);
        const easeOut = 1 - Math.pow(1 - progress, 3);
        const currentValue = Math.floor(easeOut * finalValue);

        elem.textContent = currentValue + suffix;

        if (progress < 1) {
          requestAnimationFrame(updateCounter);
        } else {
          elem.textContent = finalValue + suffix;
        }
      }

      elem.textContent = '0' + suffix;
      requestAnimationFrame(updateCounter);
    });
  }

  /* ==========================================================================
     6. LIVE COMMAND CENTER TELEMETRY CLOCK
     ========================================================================== */
  function initLiveClock() {
    const todayEl = document.querySelector('.today');
    if (!todayEl) return;

    function formatClock() {
      const d = new Date();
      const day = String(d.getDate()).padStart(2, '0');
      const months = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
      const month = months[d.getMonth()];
      const year = d.getFullYear();
      const hours = String(d.getHours()).padStart(2, '0');
      const mins = String(d.getMinutes()).padStart(2, '0');
      const secs = String(d.getSeconds()).padStart(2, '0');

      todayEl.textContent = `${day} ${month} ${year} // ${hours}:${mins}:${secs}`;
    }

    formatClock();
    setInterval(formatClock, 1000);
  }

  /* ==========================================================================
     7. MAGNETIC BUTTON INTERACTION
     ========================================================================== */
  function initMagneticButtons() {
    if (prefersReducedMotion || !window.matchMedia('(hover: hover)').matches) return;

    const magneticBtns = document.querySelectorAll('.btn.primary, .btn.secondary');

    magneticBtns.forEach((btn) => {
      btn.addEventListener('pointermove', (e) => {
        const rect = btn.getBoundingClientRect();
        const x = e.clientX - rect.left - rect.width / 2;
        const y = e.clientY - rect.top - rect.height / 2;

        btn.style.transform = `translate(${x * 0.16}px, ${y * 0.16}px)`;
      });

      btn.addEventListener('pointerleave', () => {
        btn.style.transform = '';
      });
    });
  }

  /* ==========================================================================
     8. FUTURISTIC CHART.JS RETHEME
     ========================================================================== */
  function initChartStyling() {
    if (!window.Chart) return;
    Chart.defaults.color = '#94a3b8';
    Chart.defaults.font.family = "'IBM Plex Mono', 'Space Grotesk', monospace";
  }

  /* ==========================================================================
     INITIALIZATION ON DOM LOAD
     ========================================================================== */
  document.addEventListener('DOMContentLoaded', () => {
    initHoloProgressBar();
    initAmbientParticles();
    initLogin3DScene();
    init3DCardTilt();
    initAnimatedCounters();
    initLiveClock();
    initMagneticButtons();
    initChartStyling();
  });
})();

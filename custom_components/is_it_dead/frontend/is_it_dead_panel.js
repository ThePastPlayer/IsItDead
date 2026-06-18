class IsItDeadPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._filter = "all";
    this._searchQuery = "";
    this._devices = [];
    this._expandedDevices = new Set();
  }

  set hass(val) {
    this._hass = val;
    this.render();
  }

  connectedCallback() {
    this.render();
  }

  render() {
    if (!this._hass) return;

    // Collect device-level binary sensors (v2: they have device_name attribute)
    const devices = Object.keys(this._hass.states)
      .filter(key => key.startsWith("binary_sensor."))
      .map(key => this._hass.states[key])
      .filter(state => state && state.attributes && state.attributes.device_name);

    // Sort: Dead first, Suspected second, then alphabetical by device_name
    const statusOrder = { dead: 0, suspected: 1, learning: 2, alive: 3 };
    devices.sort((a, b) => {
      const aStatus = (a.attributes.health_status || "alive").toLowerCase();
      const bStatus = (b.attributes.health_status || "alive").toLowerCase();
      const aOrder = statusOrder[aStatus] !== undefined ? statusOrder[aStatus] : 3;
      const bOrder = statusOrder[bStatus] !== undefined ? statusOrder[bStatus] : 3;
      if (aOrder !== bOrder) return aOrder - bOrder;
      const aName = a.attributes.device_name || a.entity_id;
      const bName = b.attributes.device_name || b.entity_id;
      return aName.localeCompare(bName);
    });

    this._devices = devices;

    // Compute stats
    const totalCount = devices.length;
    const deadCount = devices.filter(d => (d.attributes.health_status || "").toLowerCase() === "dead").length;
    const suspectedCount = devices.filter(d => (d.attributes.health_status || "").toLowerCase() === "suspected").length;
    const learningCount = devices.filter(d => (d.attributes.health_status || "").toLowerCase() === "learning").length;
    const healthyCount = devices.filter(d => (d.attributes.health_status || "").toLowerCase() === "alive").length;

    // Only render structure once to prevent input search losing focus
    if (!this.shadowRoot.querySelector(".panel-container")) {
      this.shadowRoot.innerHTML = `
        <style>
          :host {
            display: block;
            height: 100%;
            background-color: var(--primary-background-color, #111b21);
            color: var(--primary-text-color, #e9ecef);
            font-family: var(--paper-font-body1_-_font-family, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif);
            overflow-y: auto;
          }
          .panel-container {
            padding: 24px;
            max-width: 1600px;
            margin: 0 auto;
          }

          /* Header */
          header {
            margin-bottom: 30px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
          }
          header h1 {
            margin: 0;
            font-size: 28px;
            font-weight: 600;
            letter-spacing: -0.5px;
            background: linear-gradient(135deg, var(--primary-color, #03a9f4), #10b981);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
          }
          header p {
            margin: 4px 0 0 0;
            color: var(--secondary-text-color, #8696a0);
            font-size: 14px;
          }

          /* Stats Grid */
          .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 14px;
            margin-bottom: 24px;
          }
          .stat-card {
            background: var(--ha-card-background, var(--card-background-color, rgba(31, 44, 52, 0.8)));
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border-radius: 14px;
            padding: 16px 18px;
            border: 1px solid rgba(255, 255, 255, 0.06);
            display: flex;
            align-items: center;
            gap: 14px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
            transition: transform 0.2s ease, box-shadow 0.2s ease;
            cursor: default;
          }
          .stat-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.2);
          }
          .stat-icon {
            width: 46px;
            height: 46px;
            border-radius: 12px;
            display: flex;
            justify-content: center;
            align-items: center;
            --mdc-icon-size: 22px;
            flex-shrink: 0;
          }
          .stat-info {
            display: flex;
            flex-direction: column;
          }
          .stat-value {
            font-size: 26px;
            font-weight: 700;
            line-height: 1;
            margin-bottom: 3px;
          }
          .stat-label {
            font-size: 12px;
            color: var(--secondary-text-color, #8696a0);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            font-weight: 500;
          }

          /* Stat colors */
          .stat-total .stat-icon { background-color: rgba(3, 169, 244, 0.15); color: #03a9f4; }
          .stat-dead .stat-icon { background-color: rgba(239, 68, 68, 0.15); color: #ef4444; }
          .stat-suspected .stat-icon { background-color: rgba(249, 115, 22, 0.15); color: #f97316; }
          .stat-healthy .stat-icon { background-color: rgba(16, 185, 129, 0.15); color: #10b981; }
          .stat-learning .stat-icon { background-color: rgba(234, 179, 8, 0.15); color: #eab308; }

          /* Toolbar */
          .toolbar {
            background: var(--ha-card-background, var(--card-background-color, rgba(31, 44, 52, 0.8)));
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border-radius: 14px;
            padding: 14px 20px;
            border: 1px solid rgba(255, 255, 255, 0.06);
            margin-bottom: 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 14px;
          }
          .search-box {
            position: relative;
            flex: 1;
            min-width: 260px;
            max-width: 480px;
          }
          .search-box input {
            width: 100%;
            padding: 10px 14px 10px 40px;
            border-radius: 10px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            background-color: rgba(0, 0, 0, 0.25);
            color: var(--primary-text-color, #e9ecef);
            font-size: 14px;
            box-sizing: border-box;
            outline: none;
            transition: border-color 0.2s, box-shadow 0.2s;
          }
          .search-box input:focus {
            border-color: var(--primary-color, #03a9f4);
            box-shadow: 0 0 0 2px rgba(3, 169, 244, 0.15);
          }
          .search-box input::placeholder {
            color: var(--secondary-text-color, #8696a0);
          }
          .search-box svg {
            position: absolute;
            left: 14px;
            top: 50%;
            transform: translateY(-50%);
            width: 16px;
            height: 16px;
            fill: var(--secondary-text-color, #8696a0);
          }
          .filter-group {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
          }
          .filter-chip {
            padding: 8px 16px;
            border-radius: 20px;
            background-color: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.08);
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            color: var(--secondary-text-color, #8696a0);
            transition: all 0.2s;
            user-select: none;
          }
          .filter-chip:hover {
            background-color: rgba(255, 255, 255, 0.1);
            border-color: rgba(255, 255, 255, 0.15);
          }
          .filter-chip.active {
            background-color: var(--primary-color, #03a9f4);
            color: #fff;
            border-color: transparent;
          }

          /* Area Grouping */
          .area-section {
            margin-bottom: 32px;
          }
          .area-title-bar {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
          }
          .area-title-bar h2 {
            margin: 0;
            font-size: 20px;
            font-weight: 500;
            color: var(--primary-text-color, #e9ecef);
          }
          .area-badge {
            background-color: rgba(255, 255, 255, 0.08);
            color: var(--secondary-text-color, #8696a0);
            font-size: 12px;
            font-weight: 600;
            padding: 2px 10px;
            border-radius: 10px;
          }

          /* Device Grid */
          .devices-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
            gap: 18px;
          }

          /* Device Card */
          .device-card {
            background: var(--ha-card-background, var(--card-background-color, rgba(31, 44, 52, 0.85)));
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border-radius: 14px;
            border: 1px solid rgba(255, 255, 255, 0.06);
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
            overflow: hidden;
            display: flex;
            flex-direction: column;
            transition: transform 0.25s ease, box-shadow 0.25s ease, border-color 0.25s ease;
            position: relative;
          }
          .device-card:hover {
            transform: translateY(-3px);
            box-shadow: 0 12px 24px -6px rgba(0, 0, 0, 0.3);
          }

          /* Pulsing border for dead devices */
          @keyframes border-pulse {
            0% { border-color: rgba(239, 68, 68, 0.2); box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.08); }
            50% { border-color: rgba(239, 68, 68, 0.7); box-shadow: 0 0 12px 2px rgba(239, 68, 68, 0.15); }
            100% { border-color: rgba(239, 68, 68, 0.2); box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.08); }
          }
          .device-card.status-dead {
            animation: border-pulse 2.5s infinite;
          }
          .device-card.status-suspected {
            border-color: rgba(249, 115, 22, 0.3);
          }

          /* Card Header */
          .card-header {
            padding: 16px 18px 12px;
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 10px;
            cursor: pointer;
          }
          .card-title-block {
            display: flex;
            flex-direction: column;
            min-width: 0;
            flex: 1;
          }
          .card-device-name {
            margin: 0;
            font-size: 16px;
            font-weight: 600;
            color: var(--primary-text-color, #e9ecef);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
          }
          .card-mfr-model {
            margin: 2px 0 0 0;
            font-size: 11px;
            color: var(--secondary-text-color, #8696a0);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
          }

          /* Health Badge */
          .health-badge {
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            flex-shrink: 0;
            white-space: nowrap;
          }
          .health-badge.dead { background-color: rgba(239, 68, 68, 0.15); color: #ef4444; }
          .health-badge.suspected { background-color: rgba(249, 115, 22, 0.15); color: #f97316; }
          .health-badge.alive { background-color: rgba(16, 185, 129, 0.15); color: #10b981; }
          .health-badge.learning { background-color: rgba(234, 179, 8, 0.15); color: #eab308; }

          /* Card Body */
          .card-body {
            padding: 0 18px 14px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            flex-grow: 1;
          }

          /* Integration Pills Row */
          .integrations-row {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin-bottom: 2px;
          }
          .integration-pill {
            padding: 3px 10px;
            border-radius: 12px;
            font-size: 10px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.3px;
            background-color: rgba(99, 102, 241, 0.15);
            color: #818cf8;
            border: 1px solid rgba(99, 102, 241, 0.2);
          }
          .integration-pill.zha { background-color: rgba(16, 185, 129, 0.12); color: #34d399; border-color: rgba(16, 185, 129, 0.2); }
          .integration-pill.mqtt { background-color: rgba(139, 92, 246, 0.12); color: #a78bfa; border-color: rgba(139, 92, 246, 0.2); }
          .integration-pill.zwave_js,
          .integration-pill.zwave { background-color: rgba(6, 182, 212, 0.12); color: #22d3ee; border-color: rgba(6, 182, 212, 0.2); }
          .integration-pill.bluetooth,
          .integration-pill.ble { background-color: rgba(59, 130, 246, 0.12); color: #60a5fa; border-color: rgba(59, 130, 246, 0.2); }
          .integration-pill.esphome { background-color: rgba(245, 158, 11, 0.12); color: #fbbf24; border-color: rgba(245, 158, 11, 0.2); }
          .integration-pill.hue { background-color: rgba(244, 114, 182, 0.12); color: #f472b6; border-color: rgba(244, 114, 182, 0.2); }

          /* Info Compact Row */
          .info-row {
            display: flex;
            align-items: center;
            gap: 16px;
            flex-wrap: wrap;
            font-size: 12px;
            color: var(--secondary-text-color, #8696a0);
          }
          .info-item {
            display: flex;
            align-items: center;
            gap: 5px;
            --mdc-icon-size: 15px;
          }
          .info-item strong {
            color: var(--primary-text-color, #e9ecef);
            font-weight: 600;
          }

          /* Battery Section */
          .battery-section {
            display: flex;
            flex-direction: column;
            gap: 5px;
          }
          .battery-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 12px;
            color: var(--secondary-text-color, #8696a0);
          }
          .battery-level-container {
            display: flex;
            align-items: center;
            gap: 5px;
          }
          .battery-bar-bg {
            height: 6px;
            border-radius: 3px;
            background-color: rgba(255, 255, 255, 0.06);
            overflow: hidden;
            width: 100%;
          }
          .battery-bar-fill {
            height: 100%;
            border-radius: 3px;
            transition: width 0.4s ease;
          }
          .bat-low { background-color: #ef4444; }
          .bat-med { background-color: #f59e0b; }
          .bat-high { background-color: #10b981; }

          /* Warning Banner */
          .warning-banner {
            background-color: rgba(245, 158, 11, 0.12);
            color: #f59e0b;
            padding: 7px 12px;
            border-radius: 8px;
            font-size: 12px;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 8px;
            border: 1px solid rgba(245, 158, 11, 0.25);
          }
          .warning-banner ha-icon {
            --mdc-icon-size: 16px;
          }

          /* Detail rows */
          .detail-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 13px;
          }
          .detail-label {
            color: var(--secondary-text-color, #8696a0);
            display: flex;
            align-items: center;
            gap: 6px;
            --mdc-icon-size: 15px;
          }
          .detail-value {
            font-weight: 500;
            color: var(--primary-text-color, #e9ecef);
          }

          /* Expand Toggle */
          .expand-toggle {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            padding: 8px;
            cursor: pointer;
            font-size: 12px;
            color: var(--primary-color, #03a9f4);
            font-weight: 500;
            border-top: 1px solid rgba(255, 255, 255, 0.05);
            transition: background-color 0.2s;
            user-select: none;
          }
          .expand-toggle:hover {
            background-color: rgba(3, 169, 244, 0.06);
          }
          .expand-toggle .chevron {
            transition: transform 0.3s ease;
            display: inline-block;
          }
          .expand-toggle.expanded .chevron {
            transform: rotate(180deg);
          }

          /* Expandable Entity Details */
          .entity-details-wrapper {
            max-height: 0;
            overflow: hidden;
            transition: max-height 0.4s ease, opacity 0.3s ease;
            opacity: 0;
          }
          .entity-details-wrapper.open {
            opacity: 1;
          }
          .entity-details {
            padding: 0 18px 14px;
            display: flex;
            flex-direction: column;
            gap: 6px;
          }
          .entity-details-title {
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--secondary-text-color, #8696a0);
            margin-bottom: 4px;
          }
          .entity-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 6px 10px;
            border-radius: 8px;
            background-color: rgba(255, 255, 255, 0.02);
            font-size: 12px;
            gap: 8px;
            border: 1px solid rgba(255, 255, 255, 0.03);
          }
          .entity-row:hover {
            background-color: rgba(255, 255, 255, 0.04);
          }
          .entity-id-col {
            display: flex;
            align-items: center;
            gap: 6px;
            min-width: 0;
            flex: 1;
          }
          .entity-id-text {
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            color: var(--primary-text-color, #e9ecef);
            font-family: "Fira Code", "Cascadia Code", monospace;
            font-size: 11px;
          }
          .entity-status-icon {
            flex-shrink: 0;
            width: 16px;
            height: 16px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 13px;
          }
          .entity-status-icon.active { color: #10b981; }
          .entity-status-icon.silent { color: #f97316; }
          .entity-time-col {
            flex-shrink: 0;
            color: var(--secondary-text-color, #8696a0);
            font-size: 11px;
            text-align: right;
            white-space: nowrap;
          }

          /* Card Actions */
          .card-actions {
            padding: 10px 18px 14px;
            border-top: 1px solid rgba(255, 255, 255, 0.05);
            display: flex;
            justify-content: flex-start;
            gap: 6px;
            flex-wrap: wrap;
          }
          .action-btn-wrapper {
            position: relative;
            display: inline-flex;
          }
          .action-btn {
            background: none;
            border: 1px solid rgba(255, 255, 255, 0.08);
            color: var(--secondary-text-color, #8696a0);
            cursor: pointer;
            padding: 6px 10px;
            border-radius: 8px;
            font-size: 11px;
            font-weight: 500;
            display: inline-flex;
            align-items: center;
            gap: 5px;
            transition: all 0.2s;
            --mdc-icon-size: 14px;
          }
          .action-btn:hover {
            background-color: rgba(255, 255, 255, 0.06);
            color: var(--primary-text-color, #e9ecef);
            border-color: rgba(255, 255, 255, 0.15);
          }
          .action-btn.snoozed {
            color: #f59e0b;
            border-color: rgba(245, 158, 11, 0.3);
          }
          .action-select {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            opacity: 0;
            cursor: pointer;
            -webkit-appearance: none;
          }

          /* Empty State */
          .empty-state {
            text-align: center;
            padding: 80px 20px;
            color: var(--secondary-text-color, #8696a0);
            grid-column: 1 / -1;
          }
          .empty-state svg {
            width: 72px;
            height: 72px;
            margin-bottom: 16px;
            fill: rgba(255, 255, 255, 0.08);
          }
          .empty-state h3 {
            margin: 0 0 8px 0;
            font-size: 20px;
            font-weight: 500;
            color: var(--primary-text-color, #e9ecef);
          }
          .empty-state p {
            margin: 0;
            font-size: 14px;
          }

          /* Responsive */
          @media (max-width: 600px) {
            .panel-container { padding: 14px; }
            .devices-grid { grid-template-columns: 1fr; }
            .stats-grid { grid-template-columns: repeat(2, 1fr); }
            header h1 { font-size: 22px; }
          }
        </style>
        <div class="panel-container">
          <header>
            <div>
              <h1>Is It Dead? — Device Monitor</h1>
              <p>Device-level health monitoring with check-in anomaly detection</p>
            </div>
          </header>

          <div class="stats-grid">
            <div class="stat-card stat-total">
              <div class="stat-icon"><ha-icon icon="mdi:devices"></ha-icon></div>
              <div class="stat-info">
                <span class="stat-value" id="stat-total">0</span>
                <span class="stat-label">Total Devices</span>
              </div>
            </div>
            <div class="stat-card stat-dead">
              <div class="stat-icon"><ha-icon icon="mdi:skull-crossbones"></ha-icon></div>
              <div class="stat-info">
                <span class="stat-value" id="stat-dead">0</span>
                <span class="stat-label">Dead</span>
              </div>
            </div>
            <div class="stat-card stat-suspected">
              <div class="stat-icon"><ha-icon icon="mdi:help-circle-outline"></ha-icon></div>
              <div class="stat-info">
                <span class="stat-value" id="stat-suspected">0</span>
                <span class="stat-label">Suspected</span>
              </div>
            </div>
            <div class="stat-card stat-healthy">
              <div class="stat-icon"><ha-icon icon="mdi:heart-pulse"></ha-icon></div>
              <div class="stat-info">
                <span class="stat-value" id="stat-healthy">0</span>
                <span class="stat-label">Healthy</span>
              </div>
            </div>
            <div class="stat-card stat-learning">
              <div class="stat-icon"><ha-icon icon="mdi:brain"></ha-icon></div>
              <div class="stat-info">
                <span class="stat-value" id="stat-learning">0</span>
                <span class="stat-label">Learning</span>
              </div>
            </div>
          </div>

          <div class="toolbar">
            <div class="search-box">
              <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
              <input type="text" id="search-input" placeholder="Search device name, manufacturer, model, entity ID...">
            </div>
            <div class="filter-group">
              <button class="filter-chip active" data-filter="all">All</button>
              <button class="filter-chip" data-filter="dead">Dead</button>
              <button class="filter-chip" data-filter="suspected">Suspected</button>
              <button class="filter-chip" data-filter="alive">Healthy</button>
              <button class="filter-chip" data-filter="learning">Learning</button>
            </div>
          </div>

          <div id="devices-content"></div>
        </div>
      `;

      // Search input listener
      const searchInput = this.shadowRoot.querySelector("#search-input");
      searchInput.addEventListener("input", (e) => {
        this._searchQuery = e.target.value.toLowerCase();
        this._updateContent();
      });

      // Filter chip listeners
      const filterChips = this.shadowRoot.querySelectorAll(".filter-chip");
      filterChips.forEach(chip => {
        chip.addEventListener("click", () => {
          filterChips.forEach(c => c.classList.remove("active"));
          chip.classList.add("active");
          this._filter = chip.getAttribute("data-filter");
          this._updateContent();
        });
      });

      // Delegated event listeners on the content area
      const content = this.shadowRoot.querySelector("#devices-content");

      // Expand/collapse toggle
      content.addEventListener("click", (e) => {
        const toggle = e.target.closest(".expand-toggle");
        if (toggle) {
          const deviceId = toggle.getAttribute("data-device-entity");
          if (this._expandedDevices.has(deviceId)) {
            this._expandedDevices.delete(deviceId);
          } else {
            this._expandedDevices.add(deviceId);
          }
          this._updateContent();
          return;
        }

        // Card header click also toggles expand
        const header = e.target.closest(".card-header");
        if (header) {
          const deviceId = header.getAttribute("data-device-entity");
          if (deviceId) {
            if (this._expandedDevices.has(deviceId)) {
              this._expandedDevices.delete(deviceId);
            } else {
              this._expandedDevices.add(deviceId);
            }
            this._updateContent();
            return;
          }
        }

        // Action buttons
        const btn = e.target.closest("button[data-action]");
        if (btn) {
          const deviceEntityId = btn.getAttribute("data-device-entity");
          const trackedDeviceId = btn.getAttribute("data-tracked-device-id");
          const action = btn.getAttribute("data-action");
          if (!trackedDeviceId || !action) return;

          if (action === "exclude") {
            if (confirm("Are you sure you want to exclude this device from monitoring?")) {
              this._hass.callService("is_it_dead", "exclude_device", { device_id: trackedDeviceId });
            }
          } else if (action === "relearn") {
            if (confirm("Reset check-in history and restart learning for this device?")) {
              this._hass.callService("is_it_dead", "relearn_device", { device_id: trackedDeviceId });
            }
          }
        }
      });

      // Snooze select
      content.addEventListener("change", (e) => {
        if (e.target.classList.contains("snooze-select")) {
          const trackedDeviceId = e.target.getAttribute("data-tracked-device-id");
          const hours = parseFloat(e.target.value);
          if (!isNaN(hours) && trackedDeviceId) {
            this._hass.callService("is_it_dead", "snooze_device", {
              device_id: trackedDeviceId,
              duration_hours: hours
            });
            e.target.value = "";
          }
        }
      });
    }

    // Update stat values
    this.shadowRoot.querySelector("#stat-total").textContent = totalCount;
    this.shadowRoot.querySelector("#stat-dead").textContent = deadCount;
    this.shadowRoot.querySelector("#stat-suspected").textContent = suspectedCount;
    this.shadowRoot.querySelector("#stat-healthy").textContent = healthyCount;
    this.shadowRoot.querySelector("#stat-learning").textContent = learningCount;

    // Refresh content
    this._updateContent();
  }

  _updateContent() {
    const container = this.shadowRoot.querySelector("#devices-content");
    if (!container) return;

    const devices = this._devices;

    // Apply filter
    const filtered = devices.filter(device => {
      const status = (device.attributes.health_status || "alive").toLowerCase();

      if (this._filter !== "all" && status !== this._filter) return false;

      // Search
      if (this._searchQuery) {
        const q = this._searchQuery;
        const deviceName = (device.attributes.device_name || "").toLowerCase();
        const manufacturer = (device.attributes.manufacturer || "").toLowerCase();
        const model = (device.attributes.model || "").toLowerCase();
        const entities = (device.attributes.entities || []).join(" ").toLowerCase();
        return deviceName.includes(q) || manufacturer.includes(q) || model.includes(q) || entities.includes(q);
      }

      return true;
    });

    if (filtered.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg>
          <h3>No devices found</h3>
          <p>${this._searchQuery ? "Try refining your search query." : "No devices match the selected filter."}</p>
        </div>
      `;
      return;
    }

    // Group by area
    const groups = {};
    filtered.forEach(device => {
      const area = device.attributes.area_name || "Unassigned Area";
      if (!groups[area]) groups[area] = [];
      groups[area].push(device);
    });

    // Sort areas, Unassigned last
    const sortedAreas = Object.keys(groups).sort((a, b) => {
      if (a === "Unassigned Area") return 1;
      if (b === "Unassigned Area") return -1;
      return a.localeCompare(b);
    });

    let html = "";
    sortedAreas.forEach(area => {
      const devicesInArea = groups[area];
      const cardsHTML = devicesInArea.map(device => this._renderDeviceCard(device)).join("");

      html += `
        <div class="area-section">
          <div class="area-title-bar">
            <ha-icon icon="mdi:map-marker-outline" style="color: var(--primary-color, #03a9f4); --mdc-icon-size: 20px;"></ha-icon>
            <h2>${this._escapeHtml(area)}</h2>
            <span class="area-badge">${devicesInArea.length} device${devicesInArea.length !== 1 ? "s" : ""}</span>
          </div>
          <div class="devices-grid">
            ${cardsHTML}
          </div>
        </div>
      `;
    });

    container.innerHTML = html;

    // After render, set max-height for expanded entities
    this._expandedDevices.forEach(deviceEntityId => {
      const wrapper = container.querySelector(`.entity-details-wrapper[data-device-entity="${deviceEntityId}"]`);
      if (wrapper) {
        wrapper.style.maxHeight = wrapper.scrollHeight + "px";
        wrapper.classList.add("open");
      }
      const toggle = container.querySelector(`.expand-toggle[data-device-entity="${deviceEntityId}"]`);
      if (toggle) {
        toggle.classList.add("expanded");
      }
    });
  }

  _renderDeviceCard(device) {
    const attrs = device.attributes;
    const status = (attrs.health_status || "alive").toLowerCase();
    const deviceName = attrs.device_name || device.entity_id;
    const manufacturer = attrs.manufacturer || "";
    const model = attrs.model || "";
    const mfrModel = [manufacturer, model].filter(Boolean).join(" · ") || "Unknown device";
    const integrations = attrs.integrations || [];
    const entityCount = attrs.entity_count || 0;
    const lastActivity = attrs.last_activity;
    const lastActiveEntity = attrs.last_active_entity || "";
    const batteryLevel = attrs.battery_level;
    const batteryType = attrs.battery_type;
    const hasBatteryType = batteryType && batteryType !== "Unknown";
    const lowBatteryWarning = attrs.low_battery_warning;
    const snoozeUntil = attrs.snooze_until;
    const isSnoozed = !!snoozeUntil;
    const trackedDeviceId = attrs.tracked_device_id || "";
    const silentEntities = attrs.silent_entities || [];
    const activeEntities = attrs.active_entities || [];
    const entityDetails = attrs.entity_details || [];
    const isExpanded = this._expandedDevices.has(device.entity_id);

    // Status label
    const statusLabels = { dead: "Dead", suspected: "Suspected", alive: "Healthy", learning: "Learning" };
    const statusLabel = statusLabels[status] || "Unknown";

    // Integration pills
    const intPillsHTML = integrations.map(int => {
      const cls = int.toLowerCase().replace(/[^a-z0-9_]/g, "_");
      return `<span class="integration-pill ${cls}">${this._escapeHtml(int)}</span>`;
    }).join("");

    // Battery HTML
    let batteryHTML = "";
    if (batteryLevel !== undefined && batteryLevel !== null) {
      let batColorClass = "bat-high";
      if (batteryLevel < 20) batColorClass = "bat-low";
      else if (batteryLevel < 50) batColorClass = "bat-med";

      const batIcon = batteryLevel < 10 ? "mdi:battery-10" :
                      batteryLevel < 30 ? "mdi:battery-20" :
                      batteryLevel < 50 ? "mdi:battery-40" :
                      batteryLevel < 70 ? "mdi:battery-60" :
                      batteryLevel < 90 ? "mdi:battery-80" : "mdi:battery";

      batteryHTML = `
        <div class="battery-section">
          <div class="battery-header">
            <span class="battery-level-container">
              <ha-icon icon="${batIcon}" style="--mdc-icon-size: 16px;"></ha-icon>
              Battery${hasBatteryType ? ` (${this._escapeHtml(batteryType)})` : ""}
            </span>
            <strong>${batteryLevel}%</strong>
          </div>
          <div class="battery-bar-bg">
            <div class="battery-bar-fill ${batColorClass}" style="width: ${batteryLevel}%"></div>
          </div>
        </div>
      `;
    }

    // Warning banner
    let warningHTML = "";
    if (lowBatteryWarning) {
      warningHTML = `
        <div class="warning-banner">
          <ha-icon icon="mdi:battery-alert"></ha-icon>
          <span>Low battery warning — consider replacing soon</span>
        </div>
      `;
    }

    // Snooze info
    let snoozeHTML = "";
    if (isSnoozed) {
      const snoozeDate = new Date(snoozeUntil);
      snoozeHTML = `
        <div class="detail-row" style="color: #f59e0b;">
          <span class="detail-label" style="color: #f59e0b;"><ha-icon icon="mdi:bell-off-outline"></ha-icon> Snoozed until</span>
          <span class="detail-value" style="color: #f59e0b;">${snoozeDate.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute:'2-digit'})}</span>
        </div>
      `;
    }

    // Entity details (expandable)
    const silentSet = new Set(silentEntities);
    let entityRowsHTML = "";
    if (entityDetails.length > 0) {
      entityRowsHTML = entityDetails.map(ed => {
        const isSilent = silentSet.has(ed.entity_id);
        const statusIcon = isSilent
          ? `<span class="entity-status-icon silent" title="Silent — no recent reports">⚠️</span>`
          : `<span class="entity-status-icon active" title="Active">✅</span>`;
        const lastReported = ed.last_reported ? this._formatRelativeTime(ed.last_reported) : "Never";
        return `
          <div class="entity-row">
            <div class="entity-id-col">
              ${statusIcon}
              <span class="entity-id-text" title="${this._escapeHtml(ed.entity_id)}">${this._escapeHtml(ed.entity_id)}</span>
            </div>
            <span class="entity-time-col">${lastReported}</span>
          </div>
        `;
      }).join("");
    } else {
      // Fallback: show entity list from entities attribute
      const allEntities = attrs.entities || [];
      entityRowsHTML = allEntities.map(eid => {
        const isSilent = silentSet.has(eid);
        const statusIcon = isSilent
          ? `<span class="entity-status-icon silent" title="Silent — no recent reports">⚠️</span>`
          : `<span class="entity-status-icon active" title="Active">✅</span>`;
        return `
          <div class="entity-row">
            <div class="entity-id-col">
              ${statusIcon}
              <span class="entity-id-text" title="${this._escapeHtml(eid)}">${this._escapeHtml(eid)}</span>
            </div>
            <span class="entity-time-col">—</span>
          </div>
        `;
      }).join("");
    }

    const expandLabel = isExpanded ? "Hide entities" : `Show ${entityCount} entities`;

    return `
      <div class="device-card status-${status}">
        <div class="card-header" data-device-entity="${device.entity_id}">
          <div class="card-title-block">
            <h2 class="card-device-name" title="${this._escapeHtml(deviceName)}">${this._escapeHtml(deviceName)}</h2>
            <span class="card-mfr-model" title="${this._escapeHtml(mfrModel)}">${this._escapeHtml(mfrModel)}</span>
          </div>
          <span class="health-badge ${status}">${statusLabel}</span>
        </div>

        <div class="card-body">
          ${warningHTML}

          ${intPillsHTML ? `<div class="integrations-row">${intPillsHTML}</div>` : ""}

          ${batteryHTML}

          <div class="info-row">
            <span class="info-item">
              <ha-icon icon="mdi:cube-outline"></ha-icon>
              <strong>${entityCount}</strong> entities
            </span>
            <span class="info-item">
              <ha-icon icon="mdi:clock-outline"></ha-icon>
              ${this._formatRelativeTime(lastActivity)}
            </span>
            ${lastActiveEntity ? `
            <span class="info-item" title="Last active entity: ${this._escapeHtml(lastActiveEntity)}">
              <ha-icon icon="mdi:access-point"></ha-icon>
              <span style="max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${this._escapeHtml(lastActiveEntity.split('.').pop())}</span>
            </span>` : ""}
          </div>

          ${snoozeHTML}
        </div>

        ${entityCount > 0 ? `
        <div class="expand-toggle${isExpanded ? " expanded" : ""}" data-device-entity="${device.entity_id}">
          <span>${expandLabel}</span>
          <span class="chevron">▼</span>
        </div>

        <div class="entity-details-wrapper${isExpanded ? " open" : ""}" data-device-entity="${device.entity_id}" style="${isExpanded ? "" : "max-height: 0;"}">
          <div class="entity-details">
            <div class="entity-details-title">Entity Breakdown</div>
            ${entityRowsHTML}
          </div>
        </div>
        ` : ""}

        <div class="card-actions">
          <div class="action-btn-wrapper">
            <button class="action-btn ${isSnoozed ? 'snoozed' : ''}">
              <ha-icon icon="${isSnoozed ? 'mdi:bell-off' : 'mdi:bell-outline'}"></ha-icon>
              <span>${isSnoozed ? 'Snoozed' : 'Snooze'}</span>
            </button>
            <select class="action-select snooze-select" data-tracked-device-id="${this._escapeHtml(trackedDeviceId)}">
              <option value="" disabled selected>${isSnoozed ? 'Snoozed' : 'Snooze'}</option>
              <option value="1">1 Hour</option>
              <option value="4">4 Hours</option>
              <option value="8">8 Hours</option>
              <option value="24">24 Hours</option>
              <option value="168">7 Days</option>
              ${isSnoozed ? '<option value="0">Unsnooze</option>' : ''}
            </select>
          </div>
          <button class="action-btn" data-device-entity="${device.entity_id}" data-tracked-device-id="${this._escapeHtml(trackedDeviceId)}" data-action="exclude" title="Exclude device from monitoring">
            <ha-icon icon="mdi:eye-off-outline"></ha-icon>
            Exclude
          </button>
          <button class="action-btn" data-device-entity="${device.entity_id}" data-tracked-device-id="${this._escapeHtml(trackedDeviceId)}" data-action="relearn" title="Reset learning for this device">
            <ha-icon icon="mdi:refresh"></ha-icon>
            Re-learn
          </button>
        </div>
      </div>
    `;
  }

  _escapeHtml(str) {
    if (!str) return "";
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  _formatRelativeTime(dateString) {
    if (!dateString) return "Never";
    const date = new Date(dateString);
    if (isNaN(date.getTime())) return "Invalid";
    const now = new Date();
    const diffMs = now - date;
    if (diffMs < 0) return "Just now";
    const diffSec = Math.floor(diffMs / 1000);
    const diffMin = Math.floor(diffSec / 60);
    const diffHr = Math.floor(diffMin / 60);
    const diffDays = Math.floor(diffHr / 24);

    if (diffSec < 60) return "Just now";
    if (diffMin < 60) return `${diffMin}m ago`;
    if (diffHr < 24) return `${diffHr}h ${diffMin % 60}m ago`;
    if (diffDays < 7) return `${diffDays}d ${diffHr % 24}h ago`;
    return `${diffDays}d ago`;
  }
}

customElements.define('is-it-dead-panel', IsItDeadPanel);

class IsItDeadPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._filter = "all";
    this._searchQuery = "";
    this._deadSensors = [];
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

    const deadSensors = Object.keys(this._hass.states)
      .filter(key => key.startsWith("binary_sensor."))
      .map(key => this._hass.states[key])
      .filter(state => state && state.attributes && state.attributes.monitored_entity_id);

    // Sort: Dead first, then Alphabetical by name
    deadSensors.sort((a, b) => {
      const aIsDead = a.state === "on";
      const bIsDead = b.state === "on";
      if (aIsDead && !bIsDead) return -1;
      if (!aIsDead && bIsDead) return 1;
      const aName = a.attributes.friendly_name || a.entity_id;
      const bName = b.attributes.friendly_name || b.entity_id;
      return aName.localeCompare(bName);
    });

    // Save to instance property to avoid closure desynchronization in listeners
    this._deadSensors = deadSensors;

    // Compute stats
    const totalCount = this._deadSensors.length;
    const deadCount = this._deadSensors.filter(s => s.state === "on").length;
    const learningCount = this._deadSensors.filter(s => s.attributes.learning_active && s.state !== "on" && !s.attributes.last_reported).length;
    const healthyCount = totalCount - deadCount - learningCount;

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
            max-width: 1400px;
            margin: 0 auto;
          }
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
            font-weight: 500;
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
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
          }
          .stat-card {
            background-color: var(--ha-card-background, var(--card-background-color, #1f2c34));
            border-radius: 12px;
            padding: 16px 20px;
            border: 1px solid rgba(255, 255, 255, 0.05);
            display: flex;
            align-items: center;
            gap: 16px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
            transition: transform 0.2s ease, box-shadow 0.2s ease;
          }
          .stat-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.2);
          }
          .stat-icon {
            width: 48px;
            height: 48px;
            border-radius: 10px;
            display: flex;
            justify-content: center;
            align-items: center;
            --mdc-icon-size: 24px;
          }
          .stat-info {
            display: flex;
            flex-direction: column;
          }
          .stat-value {
            font-size: 24px;
            font-weight: 600;
            line-height: 1;
            margin-bottom: 4px;
          }
          .stat-label {
            font-size: 13px;
            color: var(--secondary-text-color, #8696a0);
            text-transform: uppercase;
            letter-spacing: 0.5px;
          }

          /* Stat colors */
          .stat-total .stat-icon { background-color: rgba(3, 169, 244, 0.15); color: #03a9f4; }
          .stat-dead .stat-icon { background-color: rgba(239, 68, 68, 0.15); color: #ef4444; }
          .stat-healthy .stat-icon { background-color: rgba(16, 185, 129, 0.15); color: #10b981; }
          .stat-learning .stat-icon { background-color: rgba(245, 158, 11, 0.15); color: #f59e0b; }

          /* Filter and Search Bar */
          .toolbar {
            background-color: var(--ha-card-background, var(--card-background-color, #1f2c34));
            border-radius: 12px;
            padding: 12px 20px;
            border: 1px solid rgba(255, 255, 255, 0.05);
            margin-bottom: 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
          }
          .search-box {
            position: relative;
            flex: 1;
            min-width: 280px;
            max-width: 450px;
          }
          .search-box input {
            width: 100%;
            padding: 10px 14px 10px 40px;
            border-radius: 8px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            background-color: rgba(0, 0, 0, 0.2);
            color: var(--primary-text-color, #e9ecef);
            font-size: 14px;
            box-sizing: border-box;
            outline: none;
            transition: border-color 0.2s;
          }
          .search-box input:focus {
            border-color: var(--primary-color, #03a9f4);
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
            border: 1px solid transparent;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            color: var(--secondary-text-color, #8696a0);
            transition: all 0.2s;
          }
          .filter-chip:hover {
            background-color: rgba(255, 255, 255, 0.1);
          }
          .filter-chip.active {
            background-color: var(--primary-color, #03a9f4);
            color: #fff;
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
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
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
            padding: 2px 8px;
            border-radius: 10px;
          }

          /* Sensors Grid */
          .sensors-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
            gap: 20px;
          }
          .sensor-card {
            background-color: var(--ha-card-background, var(--card-background-color, #1f2c34));
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.05);
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
            overflow: hidden;
            display: flex;
            flex-direction: column;
            transition: all 0.25s ease;
            position: relative;
          }
          .sensor-card:hover {
            transform: translateY(-4px);
            box-shadow: 0 12px 20px -5px rgba(0, 0, 0, 0.3);
          }
          
          /* Glowing red pulse for dead devices */
          @keyframes border-pulse {
            0% { border-color: rgba(239, 68, 68, 0.2); box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.1); }
            70% { border-color: rgba(239, 68, 68, 0.8); box-shadow: 0 0 10px 2px rgba(239, 68, 68, 0.2); }
            100% { border-color: rgba(239, 68, 68, 0.2); box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.1); }
          }
          .sensor-card.dead {
            animation: border-pulse 2.5s infinite;
          }

          .card-header {
            padding: 16px 20px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.03);
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
          }
          .card-title-container {
            display: flex;
            flex-direction: column;
            max-width: 70%;
          }
          .card-title {
            margin: 0;
            font-size: 16px;
            font-weight: 600;
            color: var(--primary-text-color, #e9ecef);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
          }
          .card-subtitle {
            margin: 2px 0 0 0;
            font-size: 11px;
            color: var(--secondary-text-color, #8696a0);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
          }
          .status-badge {
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
          }
          .status-badge.dead { background-color: rgba(239, 68, 68, 0.15); color: #ef4444; }
          .status-badge.healthy { background-color: rgba(16, 185, 129, 0.15); color: #10b981; }
          .status-badge.learning { background-color: rgba(245, 158, 11, 0.15); color: #f59e0b; }

          .card-body {
            padding: 16px 20px;
            flex-grow: 1;
            display: flex;
            flex-direction: column;
            gap: 12px;
          }

          /* Warning Banner for Low Battery */
          .warning-banner {
            background-color: rgba(245, 158, 11, 0.15);
            color: #f59e0b;
            padding: 8px 12px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 8px;
            border: 1px solid rgba(245, 158, 11, 0.3);
          }
          .warning-banner ha-icon {
            --mdc-icon-size: 16px;
          }

          /* Battery Progress Section */
          .battery-section {
            display: flex;
            flex-direction: column;
            gap: 4px;
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
            gap: 6px;
          }
          .battery-bar-bg {
            height: 6px;
            border-radius: 3px;
            background-color: rgba(255, 255, 255, 0.05);
            overflow: hidden;
            width: 100%;
          }
          .battery-bar-fill {
            height: 100%;
            border-radius: 3px;
            transition: width 0.3s ease;
          }
          
          /* Battery levels color classes */
          .bat-low { background-color: #ef4444; }
          .bat-med { background-color: #f59e0b; }
          .bat-high { background-color: #10b981; }

          /* Details Lists */
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
            --mdc-icon-size: 16px;
          }
          .detail-value {
            font-weight: 500;
            color: var(--primary-text-color, #e9ecef);
          }

          /* Estimated Depletion Alert */
          .depletion-alert {
            margin-top: auto;
            background-color: rgba(255, 255, 255, 0.02);
            border-radius: 6px;
            padding: 8px 12px;
            border-left: 3px solid var(--primary-color, #03a9f4);
            font-size: 12px;
            display: flex;
            flex-direction: column;
            gap: 2px;
          }
          .depletion-alert.stable { border-left-color: #10b981; }
          .depletion-alert.learning { border-left-color: #f59e0b; }
          .depletion-alert.no-bat { border-left-color: rgba(255, 255, 255, 0.2); }
          .depletion-title {
            font-weight: 600;
            color: var(--primary-text-color, #e9ecef);
          }
          .depletion-subtitle {
            color: var(--secondary-text-color, #8696a0);
            font-size: 11px;
          }

          /* Card Action Buttons */
          .card-actions {
            margin-top: 12px;
            padding-top: 12px;
            border-top: 1px solid rgba(255, 255, 255, 0.05);
            display: flex;
            justify-content: space-between;
            gap: 4px;
            flex-wrap: wrap;
          }
          .action-btn-wrapper {
            position: relative;
            display: inline-flex;
          }
          .action-btn {
            background: none;
            border: none;
            color: var(--secondary-text-color, #8696a0);
            cursor: pointer;
            padding: 6px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 500;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            transition: all 0.2s;
            --mdc-icon-size: 14px;
          }
          .action-btn:hover {
            background-color: rgba(255, 255, 255, 0.05);
            color: var(--primary-text-color, #e9ecef);
          }
          .action-btn.snoozed {
            color: #f59e0b;
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
            width: 80px;
            height: 80px;
            margin-bottom: 16px;
            fill: rgba(255, 255, 255, 0.1);
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
        </style>
        <div class="panel-container">
          <header>
            <div>
              <h1>Is It Dead? Surveillance Center</h1>
              <p>Dynamic battery & stale sensor monitoring powered by check-in interval anomaly detection</p>
            </div>
          </header>

          <div class="stats-grid">
            <div class="stat-card stat-total">
              <div class="stat-icon"><ha-icon icon="mdi:chart-line"></ha-icon></div>
              <div class="stat-info">
                <span class="stat-value" id="stat-total">0</span>
                <span class="stat-label">Monitored</span>
              </div>
            </div>
            <div class="stat-card stat-dead">
              <div class="stat-icon"><ha-icon icon="mdi:skull"></ha-icon></div>
              <div class="stat-info">
                <span class="stat-value" id="stat-dead">0</span>
                <span class="stat-label">Dead Sensors</span>
              </div>
            </div>
            <div class="stat-card stat-healthy">
              <div class="stat-icon"><ha-icon icon="mdi:battery-check"></ha-icon></div>
              <div class="stat-info">
                <span class="stat-value" id="stat-healthy">0</span>
                <span class="stat-label">Healthy</span>
              </div>
            </div>
            <div class="stat-card stat-learning">
              <div class="stat-icon"><ha-icon icon="mdi:cog"></ha-icon></div>
              <div class="stat-info">
                <span class="stat-value" id="stat-learning">0</span>
                <span class="stat-label">Learning</span>
              </div>
            </div>
          </div>

          <div class="toolbar">
            <div class="search-box">
              <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
              <input type="text" id="search-input" placeholder="Search by name or entity ID...">
            </div>
            <div class="filter-group">
              <button class="filter-chip active" data-filter="all">All</button>
              <button class="filter-chip" data-filter="dead">Dead</button>
              <button class="filter-chip" data-filter="learning">Learning</button>
              <button class="filter-chip" data-filter="healthy">Healthy</button>
            </div>
          </div>

          <div id="sensors-grid"></div>
        </div>
      `;

      // Setup event listeners
      const searchInput = this.shadowRoot.querySelector("#search-input");
      searchInput.addEventListener("input", (e) => {
        this._searchQuery = e.target.value.toLowerCase();
        this.updateGrid(this._deadSensors);
      });

      const filterChips = this.shadowRoot.querySelectorAll(".filter-chip");
      filterChips.forEach(chip => {
        chip.addEventListener("click", () => {
          filterChips.forEach(c => c.classList.remove("active"));
          chip.classList.add("active");
          this._filter = chip.getAttribute("data-filter");
          this.updateGrid(this._deadSensors);
        });
      });

      // Actions Event Delegation
      const grid = this.shadowRoot.querySelector("#sensors-grid");
      grid.addEventListener("click", (e) => {
        const btn = e.target.closest("button");
        if (!btn) return;

        const entityId = btn.getAttribute("data-entity");
        const action = btn.getAttribute("data-action");
        if (!entityId || !action) return;

        if (action === "exclude") {
          if (confirm(`Are you sure you want to exclude ${entityId} from monitoring?`)) {
            this._hass.callService("is_it_dead", "exclude_entity", { entity_id: entityId });
          }
        } else if (action === "relearn") {
          if (confirm(`Reset check-in history and restart learning for ${entityId}?`)) {
            this._hass.callService("is_it_dead", "relearn_entity", { entity_id: entityId });
          }
        } else if (action === "timeout") {
          const currentTimeout = btn.getAttribute("data-current-timeout");
          const hrs = prompt(`Enter manual timeout override in hours for ${entityId} (0 to use learned average):`, currentTimeout);
          if (hrs !== null) {
            const timeoutHours = parseFloat(hrs);
            if (!isNaN(timeoutHours)) {
              this._hass.callService("is_it_dead", "set_manual_timeout", {
                entity_id: entityId,
                timeout_hours: timeoutHours
              });
            }
          }
        }
      });

      grid.addEventListener("change", (e) => {
        if (e.target.classList.contains("snooze-select")) {
          const entityId = e.target.getAttribute("data-entity");
          const hours = parseFloat(e.target.value);
          if (!isNaN(hours)) {
            this._hass.callService("is_it_dead", "snooze_entity", {
              entity_id: entityId,
              duration_hours: hours
            });
            e.target.value = "";
          }
        }
      });
    }

    // Always update stat values
    this.shadowRoot.querySelector("#stat-total").textContent = totalCount;
    this.shadowRoot.querySelector("#stat-dead").textContent = deadCount;
    this.shadowRoot.querySelector("#stat-healthy").textContent = healthyCount;
    this.shadowRoot.querySelector("#stat-learning").textContent = learningCount;

    // Refresh grid cards
    this.updateGrid(this._deadSensors);
  }

  updateGrid(sensors) {
    const grid = this.shadowRoot.querySelector("#sensors-grid");
    if (!grid) return;

    // Filter sensors
    const filtered = sensors.filter(sensor => {
      const isDead = sensor.state === "on";
      const isLearning = sensor.attributes.learning_active && !isDead && !sensor.attributes.last_reported;
      const isHealthy = !isDead && !isLearning;

      // Filter matches
      if (this._filter === "dead" && !isDead) return false;
      if (this._filter === "learning" && !isLearning) return false;
      if (this._filter === "healthy" && !isHealthy) return false;

      // Search matches
      const name = (sensor.attributes.friendly_name || "").toLowerCase();
      const entityId = (sensor.attributes.monitored_entity_id || "").toLowerCase();
      if (this._searchQuery) {
        return name.includes(this._searchQuery) || entityId.includes(this._searchQuery);
      }

      return true;
    });

    if (filtered.length === 0) {
      grid.innerHTML = `
        <div class="empty-state">
          <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg>
          <h3>No sensors found</h3>
          <p>${this._searchQuery ? "Try refining your search query." : "No entities match the selected status filter."}</p>
        </div>
      `;
      return;
    }

    // Group filtered sensors by Area
    const groups = {};
    filtered.forEach(sensor => {
      const area = sensor.attributes.area_name || "Unassigned Area";
      if (!groups[area]) {
        groups[area] = [];
      }
      groups[area].push(sensor);
    });

    // Sort area names: alphabetically, but "Unassigned Area" at the bottom
    const sortedAreas = Object.keys(groups).sort((a, b) => {
      if (a === "Unassigned Area") return 1;
      if (b === "Unassigned Area") return -1;
      return a.localeCompare(b);
    });

    let html = "";
    sortedAreas.forEach(area => {
      const sensorsInArea = groups[area];
      const cardsHTML = sensorsInArea.map(sensor => {
        const isDead = sensor.state === "on";
        const isLearning = sensor.attributes.learning_active && !isDead && !sensor.attributes.last_reported;
        
        let statusLabel = "Healthy";
        let statusClass = "healthy";
        if (isDead) {
          statusLabel = "Dead";
          statusClass = "dead";
        } else if (isLearning) {
          statusLabel = "Learning";
          statusClass = "learning";
        }

        const monitoredEntityId = sensor.attributes.monitored_entity_id;
        const friendlyName = sensor.attributes.friendly_name || monitoredEntityId;
        const lastReported = sensor.attributes.last_reported;
        const reportCount = sensor.attributes.report_count || 0;
        const avgIntervalHr = sensor.attributes.average_report_interval_hours || 0;
        const timeoutThresholdHr = sensor.attributes.timeout_threshold_hours || 0;

        // Battery rendering
        const batteryLevel = sensor.attributes.battery_level;
        const batteryEntity = sensor.attributes.battery_entity_id;
        const batteryType = sensor.attributes.battery_type;
        const hasBatteryType = batteryType && batteryType !== "Unknown";
        
        let batteryHTML = "";
        if (batteryLevel !== undefined && batteryLevel !== null) {
          let batColorClass = "bat-high";
          if (batteryLevel < 20) batColorClass = "bat-low";
          else if (batteryLevel < 50) batColorClass = "bat-med";

          batteryHTML = `
            <div class="battery-section">
              <div class="battery-header">
                <span class="battery-level-container">
                  <ha-icon icon="mdi:battery" style="--mdc-icon-size: 16px;"></ha-icon>
                  Battery level ${hasBatteryType ? `(${batteryType})` : ""}
                </span>
                <strong>${batteryLevel}%</strong>
              </div>
              <div class="battery-bar-bg">
                <div class="battery-bar-fill ${batColorClass}" style="width: ${batteryLevel}%"></div>
              </div>
            </div>
          `;
        } else {
          batteryHTML = `
            <div class="battery-section">
              <div class="battery-header">
                <span class="battery-level-container" style="color: var(--secondary-text-color);">
                  <ha-icon icon="mdi:battery-off" style="--mdc-icon-size: 16px;"></ha-icon>
                  No battery sensor linked
                </span>
              </div>
            </div>
          `;
        }

        // Depletion estimates
        const depletionInfo = sensor.attributes.battery_depletion_estimate || {};
        const depletionStatus = depletionInfo.status || "Calculating depletion curve...";
        
        let depletionClass = "learning";
        if (depletionInfo.depletion_days !== undefined && depletionInfo.depletion_days !== null) {
          depletionClass = depletionInfo.depletion_days < 15 ? "dead" : "stable";
        } else if (!batteryEntity) {
          depletionClass = "no-bat";
        }

        let depletionSubtitle = "";
        if (depletionInfo.depletion_time) {
          const depDate = new Date(depletionInfo.depletion_time).toLocaleDateString(undefined, {month: 'short', day: 'numeric', year: 'numeric'});
          depletionSubtitle = `Estimated depletion: ${depDate} (${depletionInfo.discharge_rate_per_day}%/day)`;
        } else if (!batteryEntity) {
          depletionSubtitle = "Entity does not report battery state";
        } else {
          depletionSubtitle = "Requires 2+ battery level changes to calculate";
        }

        // Warning banner
        let warningBannerHTML = "";
        if (sensor.attributes.low_battery_warning) {
          warningBannerHTML = `
            <div class="warning-banner">
              <ha-icon icon="mdi:battery-alert"></ha-icon>
              <span>Battery depleting soon! (&lt; 7 days)</span>
            </div>
          `;
        }

        // Snooze state
        const snoozeUntil = sensor.attributes.snooze_until;
        const isSnoozed = !!snoozeUntil;

        return `
          <div class="sensor-card ${isDead ? "dead" : ""}">
            <div class="card-header">
              <div class="card-title-container">
                <h2 class="card-title" title="${friendlyName}">${friendlyName}</h2>
                <span class="card-subtitle" title="${monitoredEntityId}">${monitoredEntityId}</span>
              </div>
              <span class="status-badge ${statusClass}">${statusLabel}</span>
            </div>

            <div class="card-body">
              ${warningBannerHTML}
              ${batteryHTML}

              <div class="detail-row">
                <span class="detail-label"><ha-icon icon="mdi:clock-outline"></ha-icon> Last reported</span>
                <span class="detail-value">${this.formatRelativeTime(lastReported)}</span>
              </div>

              <div class="detail-row">
                <span class="detail-label"><ha-icon icon="mdi:sync"></ha-icon> Average interval</span>
                <span class="detail-value">${avgIntervalHr > 0 ? `${avgIntervalHr} hrs` : "N/A"}</span>
              </div>

              <div class="detail-row">
                <span class="detail-label"><ha-icon icon="mdi:alert-decagram-outline"></ha-icon> Timeout threshold</span>
                <span class="detail-value">${timeoutThresholdHr > 0 ? `${timeoutThresholdHr} hrs` : "N/A"}</span>
              </div>

              <div class="detail-row">
                <span class="detail-label"><ha-icon icon="mdi:chart-bar"></ha-icon> Check-ins recorded</span>
                <span class="detail-value">${reportCount}</span>
              </div>

              ${hasBatteryType ? `
              <div class="detail-row">
                <span class="detail-label"><ha-icon icon="mdi:battery-charging"></ha-icon> Battery Type</span>
                <span class="detail-value">${batteryType}</span>
              </div>
              ` : ""}

              ${isSnoozed ? `
              <div class="detail-row" style="color: #f59e0b;">
                <span class="detail-label" style="color: #f59e0b;"><ha-icon icon="mdi:bell-off-outline"></ha-icon> Snoozed until</span>
                <span class="detail-value" style="color: #f59e0b;">${new Date(snoozeUntil).toLocaleTimeString(undefined, {hour: '2-digit', minute:'2-digit'})}</span>
              </div>
              ` : ""}

              <div class="depletion-alert ${depletionClass}">
                <span class="depletion-title">${depletionStatus}</span>
                <span class="depletion-subtitle">${depletionSubtitle}</span>
              </div>

              <div class="card-actions">
                <div class="action-btn-wrapper">
                  <button class="action-btn ${isSnoozed ? 'snoozed' : ''}">
                    <ha-icon icon="${isSnoozed ? 'mdi:bell-off' : 'mdi:bell-outline'}"></ha-icon>
                    <span>${isSnoozed ? 'Snoozed' : 'Snooze'}</span>
                  </button>
                  <select class="action-select snooze-select" data-entity="${monitoredEntityId}">
                    <option value="" disabled selected>${isSnoozed ? 'Snoozed' : 'Snooze'}</option>
                    <option value="1">1 Hour</option>
                    <option value="8">8 Hours</option>
                    <option value="24">24 Hours</option>
                    <option value="168">7 Days</option>
                    ${isSnoozed ? '<option value="0">Unsnooze</option>' : ''}
                  </select>
                </div>
                <button class="action-btn" data-entity="${monitoredEntityId}" data-action="exclude" title="Exclude from monitoring">
                  <ha-icon icon="mdi:eye-off-outline"></ha-icon>
                  Exclude
                </button>
                <button class="action-btn" data-entity="${monitoredEntityId}" data-action="relearn" title="Reset learning history">
                  <ha-icon icon="mdi:refresh"></ha-icon>
                  Re-learn
                </button>
                <button class="action-btn" data-entity="${monitoredEntityId}" data-action="timeout" data-current-timeout="${timeoutThresholdHr}" title="Edit manual timeout">
                  <ha-icon icon="mdi:pencil-outline"></ha-icon>
                  Timeout
                </button>
              </div>
            </div>
          </div>
        `;
      }).join("");

      html += `
        <div class="area-section">
          <div class="area-title-bar">
            <ha-icon icon="mdi:map-marker-outline" style="color: var(--primary-color, #03a9f4); --mdc-icon-size: 20px;"></ha-icon>
            <h2>${area}</h2>
            <span class="area-badge">${sensorsInArea.length}</span>
          </div>
          <div class="sensors-grid">
            ${cardsHTML}
          </div>
        </div>
      `;
    });

    grid.innerHTML = html;
  }

  formatRelativeTime(dateString) {
    if (!dateString) return "Never";
    const date = new Date(dateString);
    const now = new Date();
    const diffMs = now - date;
    const diffSec = Math.floor(diffMs / 1000);
    const diffMin = Math.floor(diffSec / 60);
    const diffHr = Math.floor(diffMin / 60);
    const diffDays = Math.floor(diffHr / 24);

    if (diffSec < 60) return "Just now";
    if (diffMin < 60) return `${diffMin} min ago`;
    if (diffHr < 24) return `${diffHr} hrs ${diffMin % 60} min ago`;
    return `${diffDays} days ${diffHr % 24} hrs ago`;
  }
}

customElements.define("is-it-dead-panel", IsItDeadPanel);

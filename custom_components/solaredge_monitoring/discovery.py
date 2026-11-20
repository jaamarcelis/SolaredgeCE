"""Bouwt een model van de site op uit de API: welke apparaten er zijn en welke metrics ze leveren.

Niets is hardcoded: omvormers en optimizers komen uit de logische lay-out, batterijen uit de apparatuurlijst,
meters/batterij-functies uit ``components`` en de telemetriemetrics uit ``metrics/supported``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging

from .api import NotApplicableError, SolarEdgeMonitoringClient
from .api.const import DeviceType
from .api.models import (
    CniSite,
    InverterEquipmentDetails,
    LayoutNode,
    OptimizerBasicInformation,
    SiteComponents,
    SiteDetails,
    StorageDevice,
)
from .const import OPTIMIZER_BATCH_SIZE
from .metrics import MetricSpec, describe_metric, disambiguate

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class InverterModel:
    """Een omvormer met de gegevens voor ``DeviceInfo`` en de telemetrie."""

    serial: str  # volledig, bijv. 7307A2C0-DC
    identifier: str  # kort, voor analysis, bijv. 7307A2C0
    name: str
    model: str | None
    analysis_type: str  # bijv. ONE_PHASE_INVERTER
    details: InverterEquipmentDetails | None = None

    @property
    def sw_version(self) -> str | None:
        """Firmware als één tekst: DSP1, DSP2 en communicatiekaart."""
        if not self.details:
            return None
        parts = [
            f"DSP1 {self.details.dsp1}" if self.details.dsp1 else None,
            f"DSP2 {self.details.dsp2}" if self.details.dsp2 else None,
            f"COMM {self.details.comm_bord}" if self.details.comm_bord else None,
        ]
        return " / ".join(p for p in parts if p) or None


@dataclass(slots=True)
class OptimizerModel:
    """Een optimizer met paneelgegevens."""

    serial: str  # volledig, bijv. 12A33800-ED
    name: str  # bijv. Optimizer 1.1.7
    position: str | None  # bijv. 1.1.7
    inverter_serial: str | None
    model: str | None = None
    info: OptimizerBasicInformation | None = None


@dataclass(slots=True)
class SiteModel:
    """Alles wat de integratie over de site weet; bepaalt welke devices en entities er komen."""

    site_id: int
    name: str
    timezone: str
    site: CniSite
    details: SiteDetails | None
    components: SiteComponents
    inverters: list[InverterModel] = field(default_factory=list)
    optimizers: list[OptimizerModel] = field(default_factory=list)
    batteries: list[StorageDevice] = field(default_factory=list)
    # Per analysis-apparaattype de ontdekte metrics (alleen die als sensor bruikbaar zijn)
    metrics: dict[str, list[MetricSpec]] = field(default_factory=dict)

    @property
    def has_meter(self) -> bool:
        """Of de site een consumptie-/netmeter heeft."""
        return self.components.has_consumption_and_grid

    @property
    def has_storage(self) -> bool:
        """Of de site een batterij heeft."""
        return self.components.has_storage or bool(self.batteries)

    @property
    def peak_power_kw(self) -> float | None:
        """Piekvermogen in kWp."""
        if self.details and self.details.peak_power:
            return self.details.peak_power
        return self.site.peak_power_pkw / 1000 if self.site.peak_power_pkw else None

    def device_signature(self) -> tuple:
        """Vingerafdruk van de apparaten; verandert die, dan moet de integratie herladen."""
        return (
            tuple(sorted(i.serial for i in self.inverters)),
            tuple(sorted(o.serial for o in self.optimizers)),
            tuple(sorted(b.serial_number or "" for b in self.batteries)),
            self.has_meter,
            tuple(sorted((t, tuple(s.key for s in specs)) for t, specs in self.metrics.items())),
        )


async def async_discover(client: SolarEdgeMonitoringClient, site_id: int) -> SiteModel:
    """Vraag de site, apparaten en metrics op en bouw het :class:`SiteModel`."""
    site, details, components, equipment, layout, devices = await asyncio.gather(
        client.sites.get_site(site_id),
        _optional(client.dashboard.get_site_details(site_id)),
        client.dashboard.get_components(site_id),
        _optional(client.dashboard.get_equipment(site_id)),
        client.layout.get_logical_layout(site_id),
        _optional(client.analysis.get_devices(site_id)),
    )

    # Omvormers: uit de lay-out, met het analysis-type uit de apparatenlijst
    analysis_types = {d.identifier: d.device_type for d in devices or [] if d.identifier and d.device_type}
    inverters = [
        InverterModel(
            serial=node.serial or node.identifier or node.name,
            identifier=node.identifier or (node.serial or "").split("-")[0],
            name=node.name or f"Inverter {node.display_order}",
            model=node.properties.get("model") or node.properties.get("partNumber"),
            analysis_type=analysis_types.get(node.identifier or "", DeviceType.ONE_PHASE_INVERTER),
        )
        for node in layout.nodes("INVERTER")
    ]
    inverter_details = await asyncio.gather(
        *(_optional(client.dashboard.get_equipment_inverter(site_id, inv.serial)) for inv in inverters)
    )
    for inverter, info in zip(inverters, inverter_details, strict=True):
        inverter.details = info

    # Optimizers: uit de lay-out, met paneelgegevens
    optimizers = [_optimizer(layout_node, layout.parent_of(layout_node, "INVERTER")) for layout_node in layout.nodes("OPTIMIZER")]
    serials = [o.serial for o in optimizers]
    for start in range(0, len(serials), OPTIMIZER_BATCH_SIZE):
        info = await _optional(client.layout.get_optimizers_information(serials[start : start + OPTIMIZER_BATCH_SIZE]))
        by_serial = {b.serial: b for b in (info.basic_information_list if info else [])}
        for optimizer in optimizers[start : start + OPTIMIZER_BATCH_SIZE]:
            optimizer.info = by_serial.get(optimizer.serial)
            if optimizer.info and optimizer.info.model:
                optimizer.model = optimizer.info.model

    # Telemetriemetrics per omvormertype (dynamisch)
    metrics: dict[str, list[MetricSpec]] = {}
    for device_type in sorted({i.analysis_type for i in inverters}):
        supported = await _optional(client.analysis.get_device_type_metrics(site_id, device_type))
        specs: list[MetricSpec] = []
        for category in supported.supported_measurements if supported else []:
            specs.extend(
                s
                for m in category.supported_metrics
                if (s := describe_metric(category.uri, m, category_name=category.display_name)) is not None
            )
        metrics[device_type] = disambiguate(specs)

    batteries = list(equipment.storage.details) if equipment and equipment.storage else []
    model = SiteModel(
        site_id=site_id,
        name=site.name or (details.site_name if details else None) or str(site_id),
        timezone=site.timezone or "UTC",
        site=site,
        details=details,
        components=components,
        inverters=inverters,
        optimizers=optimizers,
        batteries=batteries,
        metrics=metrics,
    )
    _LOGGER.debug(
        "Site %s ontdekt: %d omvormer(s), %d optimizer(s), %d batterij(en), meter=%s, metrics=%s",
        site_id,
        len(inverters),
        len(optimizers),
        len(batteries),
        model.has_meter,
        {t: len(s) for t, s in metrics.items()},
    )
    return model


def _optimizer(node: LayoutNode, inverter: LayoutNode | None) -> OptimizerModel:
    return OptimizerModel(
        serial=node.serial or node.identifier or node.name,
        name=node.name or f"Optimizer {node.display_order}",
        position=node.display_order,
        inverter_serial=inverter.serial if inverter else None,
        model=node.properties.get("model"),
    )


async def _optional[T](awaitable: asyncio.Future[T] | object) -> T | None:
    """Wacht op ``awaitable``; geef ``None`` als het endpoint niet van toepassing is."""
    try:
        return await awaitable  # type: ignore[misc]
    except NotApplicableError as err:
        _LOGGER.debug("Niet van toepassing: %s", err)
        return None

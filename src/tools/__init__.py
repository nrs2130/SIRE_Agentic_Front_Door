"""L4 Tools: thin MCP wrappers over Vocera Engage adapters (mock simulators for the demo); I/O only."""

from .comms_page import (
    CommsAdapter,
    PageReceipt,
    create_comms_adapter,
    send_page,
)
from .comms_page import build_server as build_comms_server
from .labs_hl7 import (
    LabOrderResult,
    LabResult,
    LabsAdapter,
    create_labs_adapter,
    order_labs,
)
from .labs_hl7 import build_server as build_labs_server
from .oncall_lookup import (
    OnCallAdapter,
    OnCallLookupResult,
    OnCallProvider,
    create_oncall_adapter,
    lookup_oncall,
)
from .oncall_lookup import build_server as build_oncall_server
from .patient_context import (
    CareTeamMember,
    PatientContextAdapter,
    PatientContextResult,
    create_patient_context_adapter,
    get_patient_context,
)
from .patient_context import build_server as build_patient_context_server
from .bed_telemetry import (
    BedTelemetryAdapter,
    BedTelemetryResult,
    create_bed_telemetry_adapter,
    read_bed_telemetry,
)
from .bed_telemetry import build_server as build_bed_telemetry_server
from .monitor_alarm import (
    MonitorAdapter,
    MonitorAlarmResult,
    VitalSign,
    create_monitor_adapter,
    read_monitor,
)
from .monitor_alarm import build_server as build_monitor_server
from .blood_bank import (
    BloodBankAdapter,
    BloodBankResult,
    ProductAvailability,
    check_blood_bank,
    create_blood_bank_adapter,
)
from .blood_bank import build_server as build_blood_bank_server
from .equipment_locate import (
    EquipmentLocateAdapter,
    EquipmentLocateResult,
    EquipmentUnit,
    create_equipment_locate_adapter,
    locate_equipment,
)
from .equipment_locate import build_server as build_equipment_locate_server

__all__ = [
    # oncall_lookup
    "OnCallAdapter",
    "OnCallLookupResult",
    "OnCallProvider",
    "build_oncall_server",
    "create_oncall_adapter",
    "lookup_oncall",
    # comms_page
    "CommsAdapter",
    "PageReceipt",
    "build_comms_server",
    "create_comms_adapter",
    "send_page",
    # labs_hl7
    "LabsAdapter",
    "LabOrderResult",
    "LabResult",
    "build_labs_server",
    "create_labs_adapter",
    "order_labs",
    # patient_context
    "PatientContextAdapter",
    "PatientContextResult",
    "CareTeamMember",
    "build_patient_context_server",
    "create_patient_context_adapter",
    "get_patient_context",
    # bed_telemetry
    "BedTelemetryAdapter",
    "BedTelemetryResult",
    "build_bed_telemetry_server",
    "create_bed_telemetry_adapter",
    "read_bed_telemetry",
    # monitor_alarm (read-only)
    "MonitorAdapter",
    "MonitorAlarmResult",
    "VitalSign",
    "build_monitor_server",
    "create_monitor_adapter",
    "read_monitor",
    # blood_bank (read-only)
    "BloodBankAdapter",
    "BloodBankResult",
    "ProductAvailability",
    "build_blood_bank_server",
    "create_blood_bank_adapter",
    "check_blood_bank",
    # equipment_locate (read-only)
    "EquipmentLocateAdapter",
    "EquipmentLocateResult",
    "EquipmentUnit",
    "build_equipment_locate_server",
    "create_equipment_locate_adapter",
    "locate_equipment",
]

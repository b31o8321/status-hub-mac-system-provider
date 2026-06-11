import CoreFoundation
import Foundation
import IOKit

typealias IOHIDEventSystemClient = OpaquePointer
typealias IOHIDServiceClient = OpaquePointer
typealias IOHIDEvent = OpaquePointer

@_silgen_name("IOHIDEventSystemClientCreate")
func IOHIDEventSystemClientCreate(_ allocator: CFAllocator?) -> IOHIDEventSystemClient?

@_silgen_name("IOHIDEventSystemClientCopyServices")
func IOHIDEventSystemClientCopyServices(_ client: IOHIDEventSystemClient) -> CFArray?

@_silgen_name("IOHIDServiceClientCopyProperty")
func IOHIDServiceClientCopyProperty(_ service: IOHIDServiceClient, _ key: CFString) -> Unmanaged<CFTypeRef>?

@_silgen_name("IOHIDServiceClientCopyEvent")
func IOHIDServiceClientCopyEvent(
    _ service: IOHIDServiceClient,
    _ type: Int32,
    _ options: UInt64,
    _ timeout: UInt32
) -> IOHIDEvent?

@_silgen_name("IOHIDEventGetFloatValue")
func IOHIDEventGetFloatValue(_ event: IOHIDEvent, _ field: Int32) -> Double

private let hidProductKey = "Product" as CFString
private let temperatureEventType: Int32 = 15
private let temperatureEventField = Int32(temperatureEventType << 16)

struct SensorReading: Codable {
    let name: String
    let celsius: Double
}

struct Output: Codable {
    let source: String
    let cpuCelsius: Double?
    let maxCelsius: Double
    let sensorCount: Int
    let sensors: [SensorReading]
}

func propertyString(_ service: IOHIDServiceClient, key: CFString) -> String? {
    guard let unmanaged = IOHIDServiceClientCopyProperty(service, key) else {
        return nil
    }
    let value = unmanaged.takeRetainedValue()
    return value as? String
}

func readSensors() -> [SensorReading] {
    guard let client = IOHIDEventSystemClientCreate(kCFAllocatorDefault) else {
        return []
    }

    guard let servicesArray = IOHIDEventSystemClientCopyServices(client) else {
        return []
    }

    var readings: [SensorReading] = []
    for index in 0..<CFArrayGetCount(servicesArray) {
        guard let rawService = CFArrayGetValueAtIndex(servicesArray, index) else {
            continue
        }
        let service = OpaquePointer(rawService)
        guard let event = IOHIDServiceClientCopyEvent(service, temperatureEventType, 0, 0) else {
            continue
        }
        let celsius = IOHIDEventGetFloatValue(event, temperatureEventField)
        guard celsius > 0, celsius < 130 else {
            continue
        }
        let name = propertyString(service, key: hidProductKey) ?? "Temperature sensor"
        readings.append(SensorReading(name: name, celsius: celsius))
    }
    return readings.sorted { lhs, rhs in
        if lhs.name == rhs.name {
            return lhs.celsius < rhs.celsius
        }
        return lhs.name.localizedStandardCompare(rhs.name) == .orderedAscending
    }
}

func isLikelyCPU(_ name: String) -> Bool {
    let lowercased = name.lowercased()
    return lowercased.contains("cpu")
        || lowercased.contains("soc")
        || lowercased.contains("die")
        || lowercased.contains("pmu tdev")
        || lowercased.contains("pmu tcal")
}

let sensors = readSensors()
guard let maxReading = sensors.max(by: { $0.celsius < $1.celsius }) else {
    FileHandle.standardError.write(Data("No temperature sensors available\n".utf8))
    exit(2)
}

let cpuReadings = sensors.filter { isLikelyCPU($0.name) }
let cpuTemperature = cpuReadings.max(by: { $0.celsius < $1.celsius })?.celsius
let output = Output(
    source: "iohid",
    cpuCelsius: cpuTemperature,
    maxCelsius: maxReading.celsius,
    sensorCount: sensors.count,
    sensors: Array(sensors.sorted { $0.celsius > $1.celsius }.prefix(12))
)

let encoder = JSONEncoder()
encoder.outputFormatting = [.sortedKeys]
let data = try encoder.encode(output)
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write(Data("\n".utf8))

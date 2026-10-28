cmake_minimum_required(VERSION 3.8)
project(crab_observation_msgs)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/ObservationContext.msg"
  "msg/InputObservation.msg"
  "msg/HostVitals.msg"
  "msg/ObservationSnapshot.msg"
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()

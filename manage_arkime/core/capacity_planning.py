from dataclasses import dataclass, fields
import math
import logging
import sys
from typing import Dict, Type, TypeVar


logger = logging.getLogger(__name__)

MAX_TRAFFIC = 100 # Gbps, scaling limit of a single User Subnet VPC Endpoint
MINIMUM_NODES = 1 # We'll always have at least one capture node
MINIMUM_TRAFFIC = 0.01 # Gbps; arbitrarily chosen, but will yield a minimal cluster
CAPACITY_BUFFER_FACTOR = 1.25 # Arbitrarily chosen
MASTER_NODE_COUNT = 3 # Recommended number in docs
DEFAULT_SPI_DAYS = 30 # How many days of SPI metadata to keep in the OS Domain
DEFAULT_REPLICAS = 1 # How replicas of metadata to keep in the OS Domain
DEFAULT_HISTORY_DAYS = 365 # How many days of Arkime Viewer user history to keep in the OS Domain
DEFAULT_NUM_AZS = 2 # How many AWS Availability zones to utilize

@dataclass
class CaptureInstance:
    instanceType: str
    maxTraffic: float
    trafficPer: float
    ecsCPU: int
    ecsMemory: int

# These are the possible instances types we assign for capture nodes based on maxTraffic
CAPTURE_INSTANCES = [
    CaptureInstance("t3.medium", 0.5, 0.25, 1536, 3072),
    CaptureInstance("m5.xlarge", MAX_TRAFFIC, 2.0, 3584, 15360)
]


@dataclass
class MasterInstance:
    instanceType: str
    isArm: bool
    maxShards: int
    maxNodes: int
# These are the possible instances types we assign for master nodes based on isArm, maxShards, maxNodes
# Can't mix graviton and non-graviton instance types so we have Arm and non Arm instance types.
MASTER_INSTANCES = [
### Non-ARM
    MasterInstance("t3.small.search", False, sys.maxsize, 3),
    MasterInstance("t3.medium.search", False, sys.maxsize, 6),
    MasterInstance("m5.large.search", False, sys.maxsize, sys.maxsize),
### ARM
    MasterInstance("m6g.large.search", True, 10000, 10),
    MasterInstance("c6g.2xlarge.search", True, 30000, 30),
    MasterInstance("r6g.2xlarge.search", True, 75000, 125),
    MasterInstance("r6g.4xlarge.search", True, sys.maxsize, sys.maxsize)
]

class TooMuchTraffic(Exception):
    def __init__(self, expected_traffic: int):
        super().__init__(f"User's expected traffic ({expected_traffic} Gbps) exceeds the limit of a single cluster ({MAX_TRAFFIC})")

class NotEnoughStorage(Exception):
    def __init__(self, expected_traffic: int):
        super().__init__(f"User's expected traffic ({expected_traffic} Gbps) exceeds the limit of a OpenSearch Domain to store.")

@dataclass
class CaptureNodesPlan:
    instanceType: str
    desiredCount: int
    maxCount: int
    minCount: int

    def __equal__(self, other) -> bool:
        return (self.instanceType == other.instance_type and self.desiredCount == other.desired_count
                and self.maxCount == other.max_count and self.minCount == other.min_count)

    def to_dict(self) -> Dict[str, any]:
        return {
            "instanceType": self.instanceType,
            "desiredCount": self.desiredCount,
            "maxCount": self.maxCount,
            "minCount": self.minCount,
        }

def get_capture_node_capacity_plan(expected_traffic: float) -> CaptureNodesPlan:
    """
    Creates a capacity plan for the indicated traffic load.
    expected_traffic: The expected traffic volume for the Arkime cluster, in Gigabits Per Second (Gbps)
    """

    if not expected_traffic or expected_traffic < MINIMUM_TRAFFIC:
        expected_traffic = MINIMUM_TRAFFIC

    if expected_traffic > MAX_TRAFFIC:
        raise TooMuchTraffic(expected_traffic)

    chosen_instance = next(instance for instance in CAPTURE_INSTANCES if expected_traffic <= instance.maxTraffic)

    desired_instances = math.ceil(expected_traffic/chosen_instance.trafficPer)

    return CaptureNodesPlan(
        chosen_instance.instanceType,
        desired_instances,
        math.ceil(desired_instances * CAPACITY_BUFFER_FACTOR),
        MINIMUM_NODES
    )

@dataclass
class ViewerNodesPlan:
    maxCount: int
    minCount: int

    def __equal__(self, other) -> bool:
        return (self.desiredCount == other.desired_count
                and self.maxCount == other.max_count and self.minCount == other.min_count)

    def to_dict(self) -> Dict[str, any]:
        return {
            "maxCount": self.maxCount,
            "minCount": self.minCount,
        }

    @classmethod
    def from_dict(cls, d):
        valid_keys = {f.name for f in fields(cls)}
        valid_kwargs = {key: value for key, value in d.items() if key in valid_keys}
        return cls(**valid_kwargs)

def get_viewer_node_capacity_plan(expected_traffic: float) -> ViewerNodesPlan:
    """
    Creates a capacity plan for the indicated traffic load.
    expected_traffic: The expected traffic volume for the Arkime cluster, in Gigabits Per Second (Gbps)
    """

    if not expected_traffic or expected_traffic <= MINIMUM_TRAFFIC:
        return ViewerNodesPlan(2, 1)

    return ViewerNodesPlan(4, 2)

class UnknownInstanceType(Exception):
    def __init__(self, instance_type: str):
        super().__init__(f"Unknown instance type: {instance_type}")

@dataclass
class EcsSysResourcePlan:
    cpu: int # vCPUs; 1024 per 1 vCPU
    memory: int # in MB

    def __equal__(self, other) -> bool:
        return self.cpu == other.cpu and self.memory == other.memory

    def to_dict(self) -> Dict[str, any]:
        return {
            "cpu": self.cpu,
            "memory": self.memory
        }

def get_ecs_sys_resource_plan(instance_type: str) -> EcsSysResourcePlan:
    """
    Creates a capacity plan for the indicated instance type.
    instance_type: The instance type to plan for
    """

    chosen_instance = next((instance for instance in CAPTURE_INSTANCES if instance_type == instance.instanceType), None)
    if chosen_instance == None:
        raise UnknownInstanceType(instance_type)
    else:
        return EcsSysResourcePlan(chosen_instance.ecsCPU, chosen_instance.ecsMemory)


"""
This factor is a simplified knockdown that converts the raw packet volume to the amount of OpenSearch Domain storage.
It is based on awick@'s experience, and encompasses the following sub-factors:
* The ratio of raw-packet-data size to metadata-size
* OpenSearch indexing overhead (10%) [1]
* Linux system reserved space (5%) [1]
* OpenSearch Service overhead (20%) [1]

[1] https://docs.aws.amazon.com/opensearch-service/latest/developerguide/sizing-domains.html
"""
MAGIC_FACTOR = 0.03

@dataclass
class DataNode:
    type: str
    vol_size: int # in GiB

T3_SMALL_SEARCH = DataNode("t3.small.search", 100)
R6G_LARGE_SEARCH = DataNode("r6g.large.search", 1024)
R6G_4XLARGE_SEARCH = DataNode("r6g.4xlarge.search", 6*1024)
R6G_12XLARGE_SEARCH = DataNode("r6g.12xlarge.search", 12*1024)

@dataclass
class DataNodesPlan:
    count: int
    instanceType: str
    volumeSize: int # in GiB

    def __equal__(self, other) -> bool:
        return (self.count == other.count and self.instanceType == other.type
                and self.volumeSize == other.vol_size)

    def to_dict(self) -> Dict[str, any]:
        return {
            "count": self.count,
            "instanceType": self.instanceType,
            "volumeSize": self.volumeSize
        }

@dataclass
class MasterNodesPlan:
    count: int
    instanceType: str

    def __equal__(self, other) -> bool:
        return (self.count == other.count and self.instanceType == other.type)

    def to_dict(self) -> Dict[str, any]:
        return {
            "count": self.count,
            "instanceType": self.instanceType
        }

T_OSDomainPlan = TypeVar('T_OSDomainPlan', bound='OSDomainPlan')

@dataclass
class OSDomainPlan:
    dataNodes: DataNodesPlan
    masterNodes: MasterNodesPlan

    def __equal__(self, other) -> bool:
        return (self.dataNodes == other.dataNodes
                and self.masterNodes == other.masterNodes)

    def to_dict(self) -> Dict[str, any]:
        return {
            "dataNodes": self.dataNodes.to_dict(),
            "masterNodes": self.masterNodes.to_dict()
        }

    @classmethod
    def from_dict(cls: Type[T_OSDomainPlan], input: Dict[str, any]) -> T_OSDomainPlan:
        data_nodes = DataNodesPlan(**input["dataNodes"])
        master_nodes = MasterNodesPlan(**input["masterNodes"])
        return cls(data_nodes, master_nodes)

def _get_storage_per_replica(expected_traffic: float, spi_days: int) -> float:
    """
    Predict the required OpenSearch domain storage for each replica, in GiB

    expected_traffic: traffic volume to the capture nodes, in Gbps
    spi_days: the number of days to retain the SPI data stored in the OpenSearch Domain
    """
    return (spi_days * 24 * 60 * 60) * expected_traffic/8 * MAGIC_FACTOR

def _get_total_storage(expected_traffic: float, spi_days: int, replicas: int) -> float:
    """
    Predict the total required OpenSearch domain storage, in GiB

    expected_traffic: traffic volume to the capture nodes, in Gbps
    spi_days: the number of days to retain the SPI data stored in the OpenSearch Domain
    replicas: the number of replicas to have of the data
    """
    return _get_storage_per_replica(expected_traffic, spi_days) * (1 + replicas)

def _get_data_node_plan(total_storage: float, num_azs: int) -> DataNodesPlan:
    """
    Per the OpenSearch Service limits doc [1], you can have a maximum of 10 T2/T3 data nodes or 80 of other types by
    default.  You can raise this limit up to 200.  To keep things simple, we will assume if the user needs more storage
    than 80 of the largest instance type can provide, they'll bump the limit out of band and just keep getting more of
    that largest instance type. There's also an apparent incentive to have more, smaller nodes than fewer, larger
    nodes [2].

    We ensure there are at least two data nodes of whichever type is selected for the
    capacity plan.

    An additional constraint is that you must have an even number of data nodes if you have two AZs.

    [1] https://docs.aws.amazon.com/opensearch-service/latest/developerguide/limits.html
    [2] https://github.com/arkime/aws-aio/issues/56#issuecomment-1563652060

    total_storage: full storage requirement for all data, including replicas, in GiB
    """

    if total_storage <= 10 * T3_SMALL_SEARCH.vol_size:
        node = T3_SMALL_SEARCH
    elif total_storage <= 80 * R6G_LARGE_SEARCH.vol_size:
        node = R6G_LARGE_SEARCH
    elif total_storage <= 80 * R6G_4XLARGE_SEARCH.vol_size:
        node = R6G_4XLARGE_SEARCH
    elif total_storage <= 80 * R6G_12XLARGE_SEARCH.vol_size:
        node = R6G_12XLARGE_SEARCH
    else:
        node = R6G_12XLARGE_SEARCH # overflow with our largest instance type

    num_of_nodes = max(math.ceil(total_storage / node.vol_size), 2)
    if num_azs == 2:
        num_of_nodes = math.ceil(num_of_nodes / 2) * 2 # The next largest even integer

    plan = DataNodesPlan(
        count = num_of_nodes,
        instanceType = node.type,
        volumeSize = node.vol_size
    )

    return plan

def _get_master_node_plan(storage_per_replica: float, data_node_count: int, data_node_type: str) -> MasterNodesPlan:
    """
    We follow the sizing recommendation in the docs [1].  One complicating

    [1] https://docs.aws.amazon.com/opensearch-service/latest/developerguide/managedomains-dedicatedmasternodes.html

    storage_per_replica: storage required for each replica, in GiB
    """

    # Arkime is a write-heavy usecase so recommended data/shard is 30-50 GiB, per the docs.
    # See: https://docs.aws.amazon.com/opensearch-service/latest/developerguide/sizing-domains.html#bp-sharding
    # Although https://docs.aws.amazon.com/opensearch-service/latest/developerguide/petabyte-scale.html
    # says 100GiB is ok.
    storage_per_shard = 50 # GiB
    num_shards = math.ceil(storage_per_replica / storage_per_shard)
    isArm = not data_node_type.startswith("t3")

    chosen_instance = next(
        instance for instance in MASTER_INSTANCES if (
            isArm == instance.isArm
            and num_shards <= instance.maxShards
            and data_node_count <= instance.maxNodes
        )
    )

    return MasterNodesPlan(
        count = MASTER_NODE_COUNT,
        instanceType = chosen_instance.instanceType
    )

def get_os_domain_plan(expected_traffic: float, spi_days: int, replicas: int, num_azs: int) -> OSDomainPlan:
    """
    Get the OpenSearch Domain capacity required to satisify the expected traffic

    expected_traffic: traffic volume to the capture nodes, in Gbps
    spi_days: the number of days to retain the SPI data stored in the OpenSearch Domain
    replicas: the number of replicas to have of the data
    num_azs: the number of AZs in the domain's VPC
    """

    storage_per_replica = _get_storage_per_replica(expected_traffic, spi_days)
    total_storage = _get_total_storage(expected_traffic, spi_days, replicas)

    data_node_plan = _get_data_node_plan(total_storage, num_azs)
    master_node_plan = _get_master_node_plan(storage_per_replica, data_node_plan.count, data_node_plan.instanceType)

    return OSDomainPlan(data_node_plan, master_node_plan)

@dataclass
class CaptureVpcPlan:
    numAzs: int

    def __equal__(self, other) -> bool:
        return self.numAzs == other.numAzs

    def to_dict(self) -> Dict[str, any]:
        return {
            "numAzs": self.numAzs
        }

DEFAULT_S3_STORAGE_CLASS = "STANDARD"
DEFAULT_S3_STORAGE_DAYS = 30

@dataclass
class S3Plan:
    pcapStorageClass: str
    pcapStorageDays: int

    def __equal__(self, other) -> bool:
        return self.pcapStorageClass == other.pcapStorageClass and self.pcapStorageDays == other.pcapStorageDays

    def to_dict(self) -> Dict[str, any]:
        return {
            "pcapStorageClass": self.pcapStorageClass,
            "pcapStorageDays": self.pcapStorageDays
        }

T_ClusterPlan = TypeVar('T_ClusterPlan', bound='ClusterPlan')

@dataclass
class ClusterPlan:
    captureNodes: CaptureNodesPlan
    captureVpc: CaptureVpcPlan
    ecsResources: EcsSysResourcePlan
    osDomain: OSDomainPlan
    s3: S3Plan
    viewerNodes: ViewerNodesPlan

    def __equal__(self, other) -> bool:
        return (self.captureNodes == other.captureNodes and self.ecsResources == other.ecsResources
                and self.osDomain == other.osDomain and self.captureVpc == other.vpc and self.s3 == other.s3)

    def to_dict(self) -> Dict[str, any]:
        return {
            "captureNodes": self.captureNodes.to_dict(),
            "captureVpc": self.captureVpc.to_dict(),
            "ecsResources": self.ecsResources.to_dict(),
            "osDomain": self.osDomain.to_dict(),
            "s3": self.s3.to_dict(),
            "viewerNodes": self.viewerNodes.to_dict(),
        }

    @classmethod
    def from_dict(cls: Type[T_ClusterPlan], input: Dict[str, any]) -> T_ClusterPlan:
        capture_nodes = CaptureNodesPlan(**input["captureNodes"])
        capture_vpc = CaptureVpcPlan(**input["captureVpc"])
        ecs_resources = EcsSysResourcePlan(**input["ecsResources"])
        os_domain = OSDomainPlan.from_dict(input["osDomain"])
        s3 = S3Plan(**input["s3"])

        if "viewerNodes" in input:
            viewer_nodes = ViewerNodesPlan.from_dict(input["viewerNodes"])
        else:
            viewer_nodes = ViewerNodesPlan(4, 2)

        return cls(capture_nodes, capture_vpc, ecs_resources, os_domain, s3, viewer_nodes)


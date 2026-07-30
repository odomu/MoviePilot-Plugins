export const enabled = (key) => (config) => Boolean(config[key]);

export function createCloudDriveItems(options) {
    return options.cloudDrives?.length
        ? options.cloudDrives
        : [{title: "115网盘", value: "115"}];
}

export function createResourceTypeItems(cloudDriveItems, config) {
    const resourceTypes = [
        {title: "115分享", value: "115"},
        {title: "123分享", value: "123"},
        {title: "夸克分享", value: "quark"},
        {title: "光鸭分享", value: "guangya"},
        {title: "ED2K", value: "ed2k"},
        {title: "Magnet", value: "magnet"},
    ];
    const activeDrive = cloudDriveItems.find(
        (item) => item.value === (config.cloud_drive || "115"),
    );
    const supportedTypes = new Set(
        activeDrive?.resource_types || ["115", "ed2k", "magnet"],
    );
    return resourceTypes.filter((item) => supportedTypes.has(item.value));
}

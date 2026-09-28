// SPDX-License-Identifier: GPL-2.0-only
/* Read-only, one-shot Lenovo PGX FF-A EC packet-service diagnostic. */
#include <linux/arm_ffa.h>
#include <linux/delay.h>
#include <linux/dmi.h>
#include <linux/module.h>
#include <linux/unaligned.h>

static int packet_poll(struct ffa_device *fdev, u8 *state, u8 output[10])
{
	struct ffa_send_direct_data2 msg = {};
	u8 *raw = (u8 *)msg.data;
	int ret;

	raw[0] = 2;
	ret = fdev->ops->msg_ops->sync_send_receive2(fdev, &msg);
	if (ret)
		return ret;
	*state = raw[0];
	if (*state == 0)
		memcpy(output, raw + 1, 10);
	return 0;
}

static int lenovo_probe(struct ffa_device *fdev)
{
	struct ffa_send_direct_data2 msg = {};
	u8 *raw = (u8 *)msg.data;
	u8 output[10] = {};
	u8 state;
	u32 status;
	int ret;
	unsigned int attempt;

	if (!dmi_match(DMI_SYS_VENDOR, "LENOVO") ||
	    !dmi_match(DMI_PRODUCT_NAME, "30KL0005GF"))
		return -ENODEV;
	if (!fdev->ops || !fdev->ops->info_ops ||
	    !fdev->ops->info_ops->api_version_get || !fdev->ops->msg_ops ||
	    !fdev->ops->msg_ops->sync_send_receive2 ||
	    fdev->ops->info_ops->api_version_get() != FFA_VERSION_1_2 ||
	    fdev->mode_32bit || fdev->vm_id != 0x8003 ||
	    fdev->properties != 0x0109)
		return -EOPNOTSUPP;

	ret = packet_poll(fdev, &state, output);
	if (ret) {
		dev_err(&fdev->dev, "preflight poll transport=%d\n", ret);
		return ret;
	}
	dev_info(&fdev->dev, "preflight packet state=%u\n", state);
	if (state != 0)
		return -EBUSY;

	/* Packet operation 1 reads capability/mode/ranges; no EC write opcode. */
	raw[0] = 1;
	raw[1] = 1;
	raw[2] = 0;
	raw[3] = 10;
	ret = fdev->ops->msg_ops->sync_send_receive2(fdev, &msg);
	if (ret) {
		dev_err(&fdev->dev, "capabilities transport=%d\n", ret);
		return ret;
	}
	status = get_unaligned_le32(raw);
	dev_info(&fdev->dev, "capabilities submit status=%u\n", status);
	if (status)
		return -EREMOTEIO;

	for (attempt = 0; attempt < 100; attempt++) {
		ret = packet_poll(fdev, &state, output);
		if (ret)
			return ret;
		if (state != 2)
			break;
		msleep(10);
	}
	dev_info(&fdev->dev, "capabilities completion state=%u polls=%u payload=%*ph\n",
		 state, attempt + 1, 10, output);
	if (state != 0)
		return state == 2 ? -ETIMEDOUT : -EREMOTEIO;
	if (output[0] != 1 || output[1] != 0 ||
	    get_unaligned_le16(output + 2) != 1260 ||
	    get_unaligned_le16(output + 4) != 9000 ||
	    get_unaligned_le16(output + 6) != 1890 ||
	    get_unaligned_le16(output + 8) != 13500)
		return -EPROTO;
	return 0;
}

static void lenovo_remove(struct ffa_device *fdev)
{
	dev_info(&fdev->dev, "read-only diagnostic removed\n");
}

static const struct ffa_device_id lenovo_ids[] = {
	{ UUID_INIT(0x78b04d80, 0xd21d, 0x4986,
		    0x8a, 0xcb, 0x46, 0x7b, 0x60, 0x24, 0x7a, 0xc5) },
	{}
};
MODULE_DEVICE_TABLE(arm_ffa, lenovo_ids);

static struct ffa_driver lenovo_driver = {
	.name = "lenovo_ffa_probe",
	.probe = lenovo_probe,
	.remove = lenovo_remove,
	.id_table = lenovo_ids,
};
module_ffa_driver(lenovo_driver);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Read-only Lenovo PGX FF-A EC packet probe");
